# Board-to-Board Workflow Sequences

This reference covers sequences that move or duplicate cards between SiftLine boards, creating seamless workflow transitions.

## Table of Contents
1. Understanding Board Workflows
2. Lead Management to Acquisitions
3. Acquisitions to Transactions
4. Transactions to Closed
5. Common Patterns and Best Practices

---

## 1. Understanding Board Workflows

### The Three Core Boards

Most Sift users organize their workflow across three main boards:

| Board | Purpose | Key Phases |
|-------|---------|------------|
| Lead Management | Nurture and qualify leads | New Lead, Engage, Cold/Warm/Hot, Send to Acquisitions |
| Acquisitions | Make and negotiate offers | Make Offer, Follow Up 1-3, Accepted/Declined |
| Transactions | Manage deals to close | Under Contract, Pending Assignment, Clear to Close, Closed |

### Move vs. Duplicate

| Action | When to Use | KPI Impact |
|--------|-------------|------------|
| **Move Card** | Card should only exist on one board | Loses history on original board |
| **Duplicate Card** | Need to track progression metrics | Shows how many leads moved forward |

**Recommendation**: Use Duplicate for board-to-board transfers to preserve KPI tracking.

---

## 2. Lead Management to Acquisitions

### Trigger Point
Card moves to "Send to Acquisitions" phase on Lead Management board.

### Sequence Configuration

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | From: Lead Management, Any phase; To: Lead Management, "Send to Acquisitions" |

### Actions

1. **Duplicate Card**
   - Original: Lead Management
   - Destination: Acquisitions
   - Phase: Make Offer

2. **Assign Property**
   - Acquisitions Manager or round-robin

3. **Create Task**
   - "Make New Offer" (Due: 0 days)

### Result
- Original card stays on Lead Management board (for tracking)
- New card appears on Acquisitions board
- Acquisitions team member is assigned
- Task is created for immediate action

---

## 3. Acquisitions to Transactions

### Trigger Point
Card moves to "Offer Accepted" phase on Acquisitions board.

### Sequence Configuration

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | From: Acquisitions, Any phase; To: Acquisitions, "Offer Accepted" |

### Actions

1. **Duplicate Card**
   - Original: Acquisitions
   - Destination: Transactions
   - Phase: Under Contract

2. **Change Property Status**
   - New Status: "Under Contract"

3. **Assign Property**
   - Transaction Coordinator or round-robin

4. **Create Task**
   - "Send Contract to Title" (Due: 0 days)

5. **(Optional) Send SMS/Email**
   - Notify team of accepted offer

---

## 4. Transactions to Closed

### Trigger Point
Card moves to "Closed $$$" phase on Transactions board.

### Sequence Configuration

| Component | Setting |
|-----------|---------|
| **Trigger** | SiftLine Card Moved |
| **Condition** | From: Transactions, Any phase; To: Transactions, "Closed $$$" |

### Actions

1. **Change Property Status**
   - New Status: "Closed Deal"

2. **Add Property Tags**
   - Tag: "Closed 2024" (or current year)

3. **(Optional) Send SMS/Email**
   - Celebrate with team notification

4. **(Optional) Create Task**
   - "Post-Close Follow Up" (Due: 30 days)

---

## 5. Common Patterns and Best Practices

### Pattern: Handoff with Notification

When transferring between team members, include notification:

```
Actions:
1. Duplicate Card to new board
2. Assign Property to new team member
3. Create Task for new team member
4. Send SMS to new assignee: "New deal assigned: @property_address"
```

### Pattern: Conditional Routing

Use conditions to route to different team members:

**Sequence 1: High-Value Deals**
- Condition: Property has tag "High Value"
- Action: Assign to Senior Acquisitions Manager

**Sequence 2: Standard Deals**
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

### Best Practices

1. **Always test with one record first**: Before enabling a sequence, manually trigger it on a test record.

2. **Use consistent naming**: Name sequences clearly, e.g., "LM to ACQ - Send to Acquisitions".

3. **Document your workflow**: Keep a diagram of which sequences trigger at which phases.

4. **Check for existing cards**: Remember that "Create New Card" skips if the card already exists on the target board.

5. **Avoid sequence chains**: Sequences cannot trigger other sequences. If you need chained actions, combine them into one sequence.

### Troubleshooting Board Workflows

**Card not appearing on destination board?**
- Check if card already exists on that board
- Verify the condition matches the exact board and phase names

**Wrong team member assigned?**
- Check if round-robin is configured correctly
- Verify the assignee selection in the sequence

**Task not created?**
- Ensure task preset exists
- Check if "Assign this task to the property" is toggled correctly
