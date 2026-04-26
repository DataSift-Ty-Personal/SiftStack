# SOP — Call Scripts & Voicemail Templates

**Audience:** Mike (SMS/text outreach) and the future caller (phone outreach).
**Purpose:** Exact scripts for the most common call/SMS scenarios. Print this and have it open during calls.

---

## Core principles before any call

1. **Probate:** Speak to the **Personal Representative / Executor** (NOT the deceased). Their name is in the `Decision Maker` field in DataSift. Open with empathy — they're grieving.
2. **Foreclosure:** Speak to the **defendant** (the homeowner being foreclosed on, NOT the bank/plaintiff). Their name is in the `Owner First/Last Name` field. Open with urgency — they have a sheriff sale date approaching.
3. **Always reference the property address** — confirms you're not a robocaller and gives them something concrete to react to.
4. **Identify yourself first** — name + Wright Home Offer + how you got the info ("public records").
5. **Don't read scripts robotically** — these are skeletons. Adapt to the human on the other end.

---

## SMS Templates (Mike's primary channel)

### Probate — Day 1 SMS (Tier 1+2 phones)

```
Hi {{first_name}}, this is {{your_name}} with Wright Home Offer.
I saw you were named as the {{relationship}} on {{decedent_name}}'s
estate filing in {{county}} County. I noticed they had a property
at {{property_address}} — wondering what you're planning to do
with it. We buy houses as-is, no agent fees. Quick reply if open
to a fair cash offer.
```

**Variables:**
- `{{first_name}}` — DataSift `Decision Maker` first name
- `{{relationship}}` — `executor` / `administrator` / `personal_representative`
- `{{decedent_name}}` — DataSift `Decedent Name`
- `{{county}}` — Franklin / Montgomery / Greene
- `{{property_address}}` — DataSift `Property Street Address`

### Probate — Day 2 SMS (no response yesterday)

```
Hi {{first_name}}, following up — I know you're handling a lot
right now with {{decedent_name}}'s estate. If you'd rather sell
the {{property_address}} place quickly without dealing with
repairs, showings, or the courts longer than needed, just say
"yes" and we can chat for 5 min. No pressure either way.
```

### Probate — Day 3 SMS (final)

```
{{first_name}} — last quick note. We close in as little as 14
days, all-cash, no inspection contingencies. The estate gets
clean money fast and you can move on. Reply STOP to opt out,
or "info" for a no-obligation cash offer on {{property_address}}.
```

### Foreclosure — Day 1 SMS (Tier 1+2 phones)

```
Hi {{first_name}}, this is {{your_name}} with Wright Home Offer.
I noticed your property at {{property_address}} has a sheriff
sale scheduled for {{auction_date}}. I'm a local cash buyer in
{{county}} — if you want options before the sale (we can close
fast and stop the auction), reply and let's talk.
```

### Foreclosure — Day 2 SMS

```
{{first_name}} — wanted to circle back. The sale on {{auction_date}}
is coming up. Even if you've already worked with someone, second
opinions are free. We've helped 30+ Ohio homeowners stop
foreclosure auctions this year. Reply "info" for a quick
no-pressure conversation.
```

### Foreclosure — Day 3 SMS (urgency)

```
{{first_name}} — final follow-up before {{auction_date}}.
After the sheriff sale, your options narrow significantly.
We can close in 7-10 days, take care of all closing costs,
and you walk away with cash plus stop the foreclosure on
your record. Reply STOP to opt out, or call me at
{{your_number}}.
```

---

## Phone Scripts (for the future hire)

### Probate — Phone opener

> "Hi, is this {{first_name}}? My name is {{your_name}} and I'm with Wright Home Offer here in {{county}} County. I'm calling because I saw the probate filing for {{decedent_name}} — first off, my condolences. I know this is a tough time. I noticed there's a property associated with the estate at {{property_address}} — I wanted to reach out as a local buyer in case you're considering selling it. Do you have 60 seconds?"

**If they say no:** "Totally understand. When would be a better time to reach you?"

**If they say yes, transition to discovery:**

> "Are you the only one handling decisions on the estate, or are there siblings/family also involved? ... And what's the current state of the property — is it occupied, or sitting vacant? ... Have you thought about selling it, fixing it up, or keeping it?"

**The 4 questions you MUST get answers to:**
1. **Timeline:** When do they need this resolved? (Court deadlines, family pressure, property condition)
2. **Authority:** Are they the only decision-maker? (Single executor vs. multiple heirs)
3. **Condition:** Is the property habitable, vacant, distressed?
4. **Motivation:** What would they do with cash today? (Travel, debt, distribute to heirs)

### Foreclosure — Phone opener

> "Hi, is this {{first_name}}? My name is {{your_name}} with Wright Home Offer. I'm calling about your property at {{property_address}} — I'm a local cash buyer in {{county}} County and I noticed there's a sheriff sale scheduled for {{auction_date}}. Before that happens, I wanted to see if we can offer you a way out that protects your credit and gets you walking away with cash. Do you have a few minutes?"

**If they say "I already worked with the bank" or "I'm doing a loan mod":**

> "That's great that you're working on it. Just so you know, even if the bank gives you a forbearance, the foreclosure is already on your record once it's filed. We can structure a deal where you sell to us before the sale, the bank gets paid off, and you avoid the foreclosure showing as completed. Worth a 10-min call?"

**If they say "I'm fine, the sale isn't going to happen":**

> "Got it. Well, on the off chance things change in the next few weeks, my number is {{your_number}}. I'm a local buyer, not a national company — I close fast and don't waste your time. Best of luck with the bank."

(Tag them `not_interested` in DataSift, schedule 30-day follow-up. Sale dates can slip; many "I'm fine" calls become deals 30-90 days later.)

---

## Voicemail Templates

Always leave voicemails — they're free retargeting.

### Probate VM (15 sec — keep it tight)

> "Hi {{first_name}}, this is {{your_name}} with Wright Home Offer. I'm calling about {{decedent_name}}'s property at {{property_address}}. I'm a local buyer in {{county}} County and I'd like to make you a no-obligation cash offer on the property. My number is {{your_number}}. Again, {{your_number}}. Thanks."

### Foreclosure VM (15 sec)

> "Hi {{first_name}}, {{your_name}} from Wright Home Offer. I'm calling about your property at {{property_address}} before the sheriff sale on {{auction_date}}. I'm a local cash buyer in {{county}} and I can help you walk away with cash before the auction. {{your_number}}. {{your_number}}. Thanks."

### Day 2 / Day 3 VM (slight variation)

> "Hi {{first_name}}, {{your_name}} with Wright Home Offer following up. Just wanted to make sure my call about {{property_address}} didn't get missed. {{your_number}}. Talk soon."

---

## Objection Handling — Common Lines

### "I'm not selling."

> "Totally understand. Just curious — is there ANY scenario where the right offer would change your mind? Like, if I came in $20K above what an agent would net you after commission?"

(If still no, tag `not_interested`, schedule 90-day recycle. Real estate is emotional — minds change.)

### "Your offer is too low."

> "I appreciate you saying that. Help me understand — what number would you need to see to make this make sense?" *(let them name a number)* "Got it. Here's what I can tell you about how we calculate offers..."

(Walk them through ARV - rehab - profit margin = MAO. The 75% Rule is your friend. If they're way out of range, tell them honestly: "An agent can probably get you closer to retail. The reason cash buyers offer less is the speed and certainty — no contingencies, no inspections, no waiting. If you have time to wait 3-6 months, list it.")

### "I want to talk to my [sibling/spouse/lawyer]."

> "Smart move. When can I follow up with you after that conversation? End of this week work?"

(Tag `callback_scheduled`, set the date. If they don't get back, follow up within 7 days.)

### "How did you get my number?"

> "Public records. The {{notice_type}} filing in {{county}} County is part of the public docket — I'm a local buyer, so I track filings in case anyone in your situation needs an option. Totally up to you whether to engage."

(This is honest and disarms most "you're a scammer" reactions. Tone matters more than words.)

### "I don't trust [cash buyers / wholesalers / real estate investors]."

> "Fair concern — there are bad actors in any industry. Here's the difference: I'm based in {{county}} County, I close on my own contracts (not a wholesaler assigning), and I have references from past sellers I can put you in touch with. If you're skeptical, drive by my last 3 closings — I'll send you the addresses."

### "Send me something in writing."

> "Absolutely. I'll text you a summary right after we hang up — the property address, my offer range based on what I know today, and my phone number for anything else. Best email for a formal offer letter once we agree on terms?"

(Tag `info_sent`. Send the SMS within 5 min while you're top of mind.)

---

## When to escalate to Aaron (deal closer)

Tag `hot` in DataSift and ping Aaron when:

- Lead says "yes, send me an offer"
- Lead names a price within 10% of your MAO
- Probate executor confirms they have full authority and want to move fast (<60 days to close)
- Foreclosure homeowner agrees to a meeting before the sale date
- Any case where the deal is closeable in <14 days

**Don't escalate cold leads, "let me think about it" leads, or "send me info" leads.** Those stay in your sequential cadence.

---

## Daily call/SMS pacing (for the future caller)

- **Tier 1 (81-100):** 100% same-day SMS + same-day call. No exception.
- **Tier 2 (61-80):** Same-day SMS, call within 24 hrs.
- **Tier 3 (41-60):** SMS first, call only if no response in 48 hrs.
- **Tier 4 (21-40):** Mail-only sequence (no calls).
- **Tier 5 (0-20):** Drop. Litigator-flagged or junk number.

Realistic call pace: **40-60 calls/day** = ~3 hrs of dial time at 3-4 min/call average. Mike can comfortably send 200+ SMS/day via Launch Control or REISimpli.

---

## Variables glossary (for filling templates from DataSift)

| Template variable | DataSift field |
|---|---|
| `{{first_name}}` | Owner First Name (DM for probate, defendant for foreclosure) |
| `{{your_name}}` | Mike (or caller's name) |
| `{{your_number}}` | Wright Home Offer's published number |
| `{{decedent_name}}` | Decedent Name (probate only) |
| `{{relationship}}` | DM Relationship (executor/administrator/personal_representative) |
| `{{property_address}}` | Property Street Address |
| `{{county}}` | County (Franklin/Montgomery/Greene) |
| `{{auction_date}}` | Foreclosure Date or Tax Auction Date |
| `{{notice_type}}` | "probate filing" / "sheriff sale" / "tax sale" |

---

## See also

- [SOP-DAILY-OPERATIONS.md](SOP-DAILY-OPERATIONS.md) — Mike's morning playbook
- [SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md) — When to escalate
- [SOP-DATASIFT-NAVIGATION.md](SOP-DATASIFT-NAVIGATION.md) — Where to find these fields in DataSift UI
