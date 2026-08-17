# General Sift Operations

Day-to-day operational workflows, lead pipeline configuration, task presets, team setup, record import, and navigation patterns for REI Sift.

## Daily Operational Routine: STABM

STABM is the daily workflow framework for Sift. Run through it every working day, in order.

### S -- Status

Review and update property statuses. This is the heartbeat of your pipeline.

1. Open **Properties** from the left sidebar
2. Sort by "Last Updated" or use filter presets to find records needing status changes
3. Update statuses based on latest contact outcomes:
   - No answer after 3 calls -> consider "Not Interested" or move to mail preset
   - Positive conversation -> "Qualified" or "In Progress"
   - Offer accepted -> "Contract"
   - Deal closed -> "Closed"
   - Owner said no -> "Not Interested"
   - Owner said never call again -> "DNC"

### T -- Tasks

Work through your task queue in Events.

1. Open **Events** from the left sidebar
2. Review tasks due today (sorted by priority/deadline)
3. Complete each task and mark done -- this may trigger sequences that create the next task
4. Check overdue tasks and either complete or reschedule

### A -- Appointments (Events)

Check scheduled appointments and calls.

1. In **Events**, switch to the calendar or appointment view
2. Confirm upcoming appointments for today
3. If integrated with Google Calendar, verify sync is current

### B -- Boards (SiftLine)

Review your Kanban boards for visual pipeline health.

1. Open **SiftLine** from the left sidebar
2. Check each board: Lead Management, Acquisitions, Transactions
3. Move cards between phases as deals progress
4. Look for stale cards (stuck in one phase too long) and take action

### M -- Messages

Check and respond to incoming messages.

1. Review SMS replies (requires smrtPhone, Twilio, or Plivo integration)
2. Check email responses
3. Log any new information to the property record
4. Update status or create follow-up tasks based on responses

**Time target:** Complete STABM in 30-60 minutes each morning before outbound marketing begins.

## Lead Pipeline Configuration

### Property Statuses

These are the standard statuses used across Sift. They drive sequence triggers, filter presets, and board phases.

| Status | Purpose | When to Apply |
|---|---|---|
| New Lead | Record just imported, no contact attempt yet | On upload or first entry into system |
| Qualified | Lead meets investment criteria after initial contact | After first positive conversation or research confirms potential |
| In Progress | Active negotiations, due diligence, or deal-making | After qualification, while working the deal |
| Contract | Under contract, heading toward closing | After signed purchase agreement |
| Closed | Deal completed successfully | After closing table |
| Sold | Property sold on market (not your deal) | Market status change detected, or manual update |
| Dead | Deal fell through after being in progress | Contract fell through, financing failed, title issues |
| Not Interested | Owner explicitly declined to sell | After clear "no" from owner |
| DNC | Do Not Contact -- legal/compliance requirement | Owner requests no further contact, or number is on DNC registry |

### Status Transition Map

Common progressions (arrows show typical flow):

```
New Lead -> Qualified -> In Progress -> Contract -> Closed
    |           |             |
    v           v             v
Not Interested  Dead         Dead
    |
    v
DNC (if requested)
```

**Reactivation paths:**
- Not Interested -> New Lead (quarterly re-engagement via preset 11)
- Dead -> In Progress (if deal circumstances change)

### Status Change Triggers for Sequences

When a status changes, sequences listening for that trigger will fire. Common patterns:

| Status Change | Typical Sequence Action |
|---|---|
| Any -> New Lead | Create "Call New Lead" task, add to Lead Management board, start drip |
| New Lead -> Qualified | Create "Make Offer" task, move card to Qualified phase |
| Qualified -> In Progress | Create follow-up task, notify acquisitions team |
| In Progress -> Contract | Move to Transactions board, create closing checklist tasks |
| Any -> Sold | Remove from all lists, clear tasks, clear assignee (Sold Cleanup sequence) |
| Any -> Not Interested | Clear active tasks, schedule quarterly re-engagement |
| Any -> DNC | Remove from all lists, clear tasks, remove from drip campaigns |

## Task Presets

Task presets are reusable templates that sequences use to create tasks automatically. Configure them at **Events -> Configure Presets**.

### Standard Task Presets

| Preset Name | Purpose | Typical Deadline |
|---|---|---|
| Call New Lead | First contact attempt on a fresh lead | Same day (speed-to-lead) |
| No Contact New Lead | Follow-up when first call got no answer | Next business day |
| Nurture New Lead | Long-term follow-up for leads not yet ready | 7 days out |
| Cold Follow-Up | Re-engage a cold lead with no recent activity | 14-30 days out |
| Warm Follow-Up | Follow up with a lead showing moderate interest | 3-7 days out |
| Hot Follow-Up | Urgent follow-up with a highly motivated seller | Same day or next day |
| Make Offer | Prepare and present offer to qualified lead | 1-3 days out |
| Offer Follow-Up | Check in after presenting offer | 2-3 days after offer |
| Send Back to LM | Return lead to Lead Management for re-marketing | Same day |

### Creating a Task Preset

1. Go to **Events** in the left sidebar
2. Click **Configure Presets** (gear icon)
3. Click **"Add New Preset"**
4. Fill in:
   - **Preset Name**: Use the names above or customize
   - **Description**: What the task involves (e.g., "Call all phone numbers, log outcome")
   - **Default Duration**: How long the task should take
   - **Priority**: Low, Medium, High, or Urgent
5. Click **Save**

### Linking Task Presets to Sequences

In any sequence action:
1. Drag "Create New Task" action
2. Select the task preset from the dropdown
3. Toggle "Assign this task to the property" ON
4. Optionally set "Assign to" (specific user or round-robin)

## Team Role Assignment and Permissions

### Sift User Roles

| Role | Can Create Sequences | Can Edit Presets | Record Visibility | Best For |
|---|---|---|---|---|
| Sensei | Yes | Yes | All records | Platform owner |
| Super Admin | Yes | Yes | All records | Operations manager |
| Admin | Yes | Yes | All records | Team lead |
| Marketer | Yes | Yes | All records | Marketing coordinator |
| Acquisitions | No | No | Assigned records only | Acquisition agents |
| Dispositions | No | No | Assigned records only | Disposition agents |
| Researchers | No | No | Assigned records only | Skip tracers, researchers |
| Prospectors | No | No | Assigned records only | Deep prospecting team |

**Critical note for restricted roles (Acquisitions, Dispositions, Researchers, Prospectors):** These users can ONLY see records assigned to them. When a sequence creates a task for a restricted user, the sequence must ALSO assign the property record to that user, or they will not be able to see the record or the task.

### Round-Robin Assignment

Sequences can auto-distribute leads across team members:
1. In a sequence action, select "Assign Property"
2. Choose "Round Robin" mode
3. Select the team members to rotate between
4. Leads will be distributed evenly in order

## Speed-to-Lead

**The #1 operational rule: Contact new leads within 1 minute of import.**

Industry data shows that contacting a lead within 1 minute of receiving their information results in a **4x higher close rate** compared to waiting even 5 minutes.

### How to Implement Speed-to-Lead in Sift

1. **Sequence trigger:** Property Status Change -> To "New Lead"
2. **Action 1:** Create Task from "Call New Lead" preset (due: immediately)
3. **Action 2:** Send SMS notification to acquisitions agent ("New lead assigned: [address]")
4. **Action 3:** Add to SiftLine Lead Management board, "New" phase

The moment a record hits "New Lead" status (via upload or manual entry), the agent gets a task and an SMS alert. No delay.

### Speed-to-Lead Checklist

- [ ] "Call New Lead" task preset exists with same-day deadline
- [ ] Sequence fires on status change to "New Lead"
- [ ] SMS notification goes to assigned agent
- [ ] Agent phone is set up for click-to-dial
- [ ] Agent knows to prioritize new lead tasks above all others

## Record Import Workflow

### CSV Upload Process

1. **Prepare CSV**: Ensure columns match Sift's field mapping (Property Street, City, State, ZIP, Owner First Name, Owner Last Name at minimum)
2. **Navigate**: Left sidebar -> Upload File -> "Add Data"
3. **Select upload type**: "Uploading a new list not in DataSift yet" (for new data) or select existing list (for updates)
4. **Enter list name**: Use descriptive names (e.g., "Knox Foreclosure 2025-04")
5. **Skip tags step**: Tags should be in your CSV column, not manually added here
6. **Upload file**: Select your CSV file
7. **Map columns**: Core address fields usually auto-map. Manually map: Tags, Lists, Notes, Estimated Value, and any custom fields
8. **Review and finish**: Click "Finish Upload" -- processing runs in background

### Post-Upload Checklist

After every upload:

1. **Verify record count**: Check that the expected number of records appeared
2. **Spot-check mapping**: Open 2-3 records and verify fields mapped correctly
3. **Run enrichment** (if using): Manage -> Enrich Data -> select your list. Keep "Enrich Owners" and "Swap Owners" OFF to protect your contact mapping
4. **Run skip trace**: Send To -> Skip Trace -> select your list. This pulls phone numbers and emails
5. **Verify sequences fired**: If your upload set a status (e.g., "New Lead"), check that the expected sequences triggered by reviewing the Activity Log on a sample record

### Upload Triggers -- Important Limitation

**Only Property Status Change triggers work reliably with CSV uploads.** Tag-added and list-added triggers fire only on manual additions, not on bulk uploads. If you need automations to fire on upload, set a default status during the upload process.

## Common Status Transitions and Their Triggers

### Automated (via Sequences)

| Trigger Event | Automated Status Change | Sequence Folder |
|---|---|---|
| Record uploaded with no status | -> New Lead | Lead Management |
| Task "Call New Lead" completed, marked "No Answer" | -> stays New Lead, creates "No Contact" task | Lead Management |
| Task "No Contact" completed 3x | -> creates "Needs Mailed" task | Lead Management |
| Owner says "interested" | -> Qualified (manual update triggers sequence) | Lead Management |
| Offer accepted | -> Contract (manual update triggers sequence) | Acquisitions |
| Property Tags: "Sold" added | -> Sold status, clear lists/tasks/assignee | Transactions |

### Manual (Agent Decisions)

These transitions require human judgment and should NOT be fully automated:

- New Lead -> Qualified: Agent confirms motivation and property potential
- Qualified -> In Progress: Agent decides to pursue the deal
- In Progress -> Contract: Offer accepted, contract signed
- Contract -> Closed: Closing completed
- Any -> Dead: Agent determines deal is not viable
- Any -> DNC: Owner requests no further contact (legal compliance)

## Navigation Quick Reference

| Action | Path |
|---|---|
| View all records | Left sidebar -> Properties |
| Open filter panel | Properties -> filter icon (top bar) |
| Load a filter preset | Filter panel -> scroll to bottom -> Filter Presets -> select folder -> click preset |
| Create new sequence | Left sidebar -> Sequences -> Create New Sequence |
| View tasks | Left sidebar -> Events |
| Configure task presets | Events -> gear icon -> Configure Presets |
| Open SiftLine boards | Left sidebar -> SiftLine |
| Upload records | Left sidebar -> Upload File -> Add Data |
| Run skip trace | Properties -> select records -> Send To -> Skip Trace |
| Run enrichment | Properties -> Manage -> Enrich Data |
| View activity log | Open any property record -> Activity Log tab |

## Default Account Inventory (Post-4/16/2025)

New Sift accounts include:

### Pre-Built Boards (SiftLine)
- Lead Management (phases: New, Contacted, Qualified, Follow-Up)
- Acquisitions (phases: Analyzing, Offer Sent, Negotiating, Under Contract)
- Transactions (phases: Title, Inspection, Financing, Closing)

### Pre-Built Sequences (26 total)
- **Lead Management folder (6):** Intake, follow-up chain, temperature changes, re-engagement
- **Acquisitions folder (6):** Offer workflow, acceptance/rejection handling, escalation
- **Transactions folder (6):** Contract-to-close workflow, closing checklist, post-close cleanup, Sold Property Cleanup
- **Deep Prospecting folder (4):** Research assignment, findings review, re-engagement triggers
- **Default folder (4):** Basic status-change responses, catch-all automations

### Pre-Built Filter Presets (21 total)
- **00 Niche Sequential Marketing (12):** Presets 00-11 for first-to-market data
- **01. Bulk Sequential Marketing (9):** Presets 00-08 for stacked/bulk data

### Tags (Standard)
- `courthouse data` -- first-to-market county records
- `skip_traced_YYYY-MM` -- auto-applied after skip trace
- `return mail` -- mail piece returned, bad address
- Notice type tags: `foreclosure`, `probate`, `tax_sale`, `tax_delinquent`, `eviction`, `code_violation`, `divorce`
- County tags: `Knox`, `Blount`

### Lists (Standard)
- Foreclosure, Probate, Tax Sale, Tax Delinquent, Eviction, Code Violation, Divorce
- Lists auto-create from the "Lists" column in uploaded CSVs
