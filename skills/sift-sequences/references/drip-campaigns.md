# Drip Campaigns with Sequences

This reference covers how to use Drip Campaigns in conjunction with Sequences for long-term lead nurture.

## Table of Contents
1. Drip Campaigns vs. Sequence Actions
2. Setting Up Drip Campaigns
3. Connecting Drips to Sequences
4. Drip Campaign Templates
5. Managing and Troubleshooting

---

## 1. Drip Campaigns vs. Sequence Actions

### Key Differences

| Feature | Sequence SMS/Email | Drip Campaigns |
|---------|-------------------|----------------|
| **Timing** | Immediate | Delayed (minutes, hours, days) |
| **Best For** | Instant notifications | Long-term nurture |
| **Complexity** | Single message | Multi-step sequences |
| **Delay Options** | None | Minutes, hours, days |
| **SMS Integration** | Any provider | smrtPhone, Twilio, Plivo only |

### When to Use Each

**Use Sequence SMS/Email when:**
- You need immediate notification
- Alerting team members of new leads
- Sending one-time confirmations
- Time-sensitive communications

**Use Drip Campaigns when:**
- Following up over days or weeks
- Re-engaging cold or dead leads
- Nurturing leads not ready to sell
- Creating automated follow-up sequences

---

## 2. Setting Up Drip Campaigns

### Prerequisites

Before creating drip campaigns:

1. **SMS Integration**: Must have smrtPhone, Twilio, or Plivo connected
   - NOT available with Kixie, Smarter Contact, or Launch Control

2. **Account Timezone**: Set in Settings > Profile
   - Messages only send between 8 AM and 9 PM in your timezone

3. **Compliance**: Must comply with A2P 10DLC regulations

### Creating a Drip Campaign

1. Navigate to "Drip Campaigns" in left sidebar
2. Click "Add New Campaign"
3. Name your campaign (e.g., "Cold Lead Re-engagement")
4. Drag and drop steps:
   - SMS
   - Email
   - Task
   - Delay
5. Configure each step
6. Save the campaign

### Delay Options

| Delay Type | Use Case |
|------------|----------|
| Minutes | Quick follow-up sequences |
| Hours | Same-day follow-ups |
| Days | Long-term nurture campaigns |

---

## 3. Connecting Drips to Sequences

### Adding Drip Campaign Action to Sequence

1. Open or create a sequence
2. In the Actions section, select "Add new Action"
3. Drag and drop "Drip Campaign"
4. Select the drip campaign to trigger
5. Save the sequence

### Example: New Lead Drip Sequence

```
Trigger: Property Status Change
Condition: From Any to New Lead

Actions:
  1. Assign Property to Lead Manager
  2. Create Card on Lead Management board
  3. Create Task: "Call New Lead" (Due: 0 days)
  4. Add to Drip Campaign: "New Lead Welcome"
```

### Example: Dead Lead Revival Sequence

```
Trigger: Property Status Change
Condition: From Any to Dead Lead

Actions:
  1. Move Card to "Dead" phase
  2. Add to Drip Campaign: "Dead Lead Re-engagement"
```

---

## 4. Drip Campaign Templates

### Template: New Lead Welcome (7 days)

| Step | Type | Delay | Content |
|------|------|-------|---------|
| 1 | SMS | Immediate | "Hi @contact_first_name, this is @user_first_name. I noticed you might be interested in selling @property_address. Is now a good time to chat?" |
| 2 | Delay | 1 day | - |
| 3 | SMS | After delay | "Just following up on @property_address. I'd love to discuss options that work for your timeline." |
| 4 | Delay | 2 days | - |
| 5 | SMS | After delay | "Hi @contact_first_name, I'm still interested in @property_address if you're open to a quick conversation." |
| 6 | Delay | 3 days | - |
| 7 | Task | After delay | "Call Lead - Final Attempt" |

### Template: Cold Lead Re-engagement (30 days)

| Step | Type | Delay | Content |
|------|------|-------|---------|
| 1 | SMS | Immediate | "Hi @contact_first_name, checking in on @property_address. Has anything changed with your situation?" |
| 2 | Delay | 7 days | - |
| 3 | SMS | After delay | "Just wanted to reach out again about @property_address. I'm here if you'd like to explore options." |
| 4 | Delay | 14 days | - |
| 5 | SMS | After delay | "Hi @contact_first_name, I know timing is everything. If your situation has changed, I'd love to help with @property_address." |
| 6 | Delay | 7 days | - |
| 7 | Task | After delay | "Review Cold Lead Status" |

### Template: Dead Lead Revival (90 days)

| Step | Type | Delay | Content |
|------|------|-------|---------|
| 1 | SMS | Immediate | "Hi @contact_first_name, it's been a while since we spoke about @property_address. Just checking if anything has changed." |
| 2 | Delay | 30 days | - |
| 3 | SMS | After delay | "Hi @contact_first_name, still thinking about @property_address. Let me know if you'd like to revisit our conversation." |
| 4 | Delay | 30 days | - |
| 5 | SMS | After delay | "Hi @contact_first_name, reaching out one more time about @property_address. I'm here whenever you're ready." |
| 6 | Delay | 28 days | - |
| 7 | Task | After delay | "Dead Lead 90-Day Review" |

---

## 5. Managing and Troubleshooting

### Viewing Drip Campaign Status

Click "View Details" on any campaign to see:

| Status | Meaning |
|--------|---------|
| Active | Currently being processed |
| Completed | Finished all steps |
| Failed | Error (usually missing phone/email) |
| Removed | Manually removed from campaign |

### Removing Records from Drips

**From within the campaign:**
1. Click "View Details"
2. Find the record
3. Click three dots
4. Select "Remove from campaign"

**From within the record:**
1. Open the property record
2. Find "Campaigns" section
3. Click "Remove"

### Common Issues

**Drip not sending?**
- Check if phone number is valid
- Verify SMS integration is active
- Confirm account timezone is set
- Check if message is within 8 AM - 9 PM window

**Wrong phone number receiving messages?**
- Verify "Send To" selection in drip step
- Check which phone field is populated on the record

**Drip campaign not triggering from sequence?**
- Verify sequence is toggled ON
- Check if conditions are met
- Review Activity Log for errors

### Best Practices

1. **Keep messages short**: SMS should be concise and actionable

2. **Use variables**: Personalize with @contact_first_name, @property_address, etc.

3. **Space out messages**: Don't overwhelm leads with too many messages

4. **End with a task**: Create a task at the end of drips for manual follow-up

5. **Monitor failed drips**: Regularly check for failed messages and fix data issues

6. **Comply with regulations**: Follow A2P 10DLC guidelines for SMS
