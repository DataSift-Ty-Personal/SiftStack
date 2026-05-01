# SOP — Daily Outreach Cadence by Preset

**Audience:** Mike (text outreach), future caller (phone outreach), Aaron (closer).
**Purpose:** Day-by-day playbook for working each preset. Tells Mike exactly what to do on Day 1, Day 2, Day 7, Day 30 — across SMS, phone, mail. Frames every touchpoint as "we have options to help" — not a sales pitch.

---

## Core philosophy — we lead with options, not offers

Distress homeowners get pitched constantly by wholesalers and predators. What sets us apart is the **first sentence**:

> "I'm a local buyer in your county. Based on where your situation is now, I can present a few options — not all of them require selling. What's worth knowing?"

That single framing changes the response rate dramatically. We're consultative, they pick the path.

**The 4 options we always have:**

| Option | When it fits | What we do |
|---|---|---|
| **Cash sale** | Owner wants speed + cash, no equity to fight for | Buy as-is, close 7-14 days, no fees |
| **Fund redemption** | Already past sheriff sale, redemption window open | Provide cash to redeem, then buy from them — they walk away with money instead of nothing |
| **Refer to attorney / loan mod** | Owner has equity + wants to keep house | Free referral to bankruptcy attorney or loan mod specialist; we don't profit, we win goodwill |
| **Just answer questions** | Confused / overwhelmed | Free 10-min phone call to explain the foreclosure process, what comes next, what their rights are |

**Mike's script always opens with "I have a few options to help" — never "I want to buy your house".**

---

## Universal cadence framework

Every distress lead gets this base cadence unless they STOP, convert, or hard-decline:

```
Day 0   Records lands in DataSift (autonomous)
Day 1   SMS — opening message + "options to help" framing
Day 2   SMS — soft follow-up if no response Day 1
Day 3   SMS — final urgency (event-based: sale date, court deadline)
Day 4   Phone call attempt (Tier 0-2 numbers only)
Day 7   SMS — nurture check-in
Day 14  Phone call attempt #2 + mailer #1
Day 30  Phone call attempt #3
Day 60  Mailer #2
Day 90  Recycle through cadence (only for status=Dead, never for status=Sold)
```

**Cadence overrides per preset:** see each preset section below. The above is baseline.

---

## When to STOP the cadence

Stop touchpoints permanently for any of these:

- ✋ **Reply STOP** — TCPA opt-out. Tag `do_not_contact`. Never message again.
- ✋ **Status: Sold** — sold to someone else. Tag `Sold`. Sold Property Cleanup sequence fires.
- ✋ **Status: Closed** — we closed the deal. Move to post-close workflow.
- ✋ **Hard decline + reason** — "I'm staying, my son's moving back." Note in Notes, recycle in 90 days only if reason changes.
- ✋ **Returned mail (3x)** — bad address. Tag `bad_address`, drop.

---

# Preset-Specific Cadences

---

## §1-3. Lis Pendens — Montgomery / Franklin / Greene

**Situation:** Foreclosure case JUST filed in Common Pleas. Sheriff sale is 4-12 weeks away. Homeowner is in early panic mode. **They have the most options of any distress lead — most time, most leverage, most paths forward.**

### Tone

Calm, advisor-mode. Not urgent yet. Lead with "you have time and options" — distinguishes us from sheriff-sale-day vultures.

### Cadence

| Day | Action | Channel | Template/Script |
|---|---|---|---|
| **Day 1** | Send opening SMS — "options to help" frame | SMS | [SOP-SMS Templates §1/§2/§3](SOP-SMS-TEMPLATES-BY-PRESET.md#1-lis-pendens--montgomery) Day 1 primary |
| **Day 2** | Soft follow-up if no response | SMS | §1/§2/§3 Day 2 |
| **Day 3** | Brief "options" reminder, no urgency | SMS | §1/§2/§3 Day 3 |
| **Day 4** | First phone call (Tier 0-2 only) | Phone | LP phone opener (below) |
| **Day 7** | Nurture text — share resource | SMS | "Hey {{first}}, FYI Ohio has a 30-day right-to-cure window after lis pendens. Here's what that means: [link]. {{phone}} if questions." |
| **Day 14** | Second phone attempt + mailer #1 | Phone + Mail | Mail piece: "Options Letter — Lis Pendens Filed" |
| **Day 21** | Third phone attempt | Phone | Reference any conversation, sale-date check |
| **Day 30** | Update — has sheriff sale been scheduled yet? | SMS | "{{first}}, has a sheriff sale date been set yet on {{address}}? Wanted to check in." |
| **Day 60** | Mailer #2 + final phone attempt | Phone + Mail | Mail piece: "Final Options Before Sheriff Sale" |
| **Day 90** | Move to nurture-only quarterly mailer | Mail | Quarterly check-in piece |

### LP Phone Opener (Day 4)

> "Hi {{first}}, this is {{me}} from Wright Home Offer in {{county}} County. I saw your foreclosure case was filed and I wanted to reach out — not because I'm trying to sell you anything, but because I work with folks in your spot every day and there are usually 3-4 options most people don't know about. Do you have 5 minutes? I'm happy to walk through them and you can pick the right path for you."

**Key phrases:**
- "Not trying to sell you anything"
- "3-4 options most people don't know about"
- "You can pick the right path"

### Solutions to offer in LP conversation

1. **Cash sale** — fastest, walk away clean (best if they're underwater or burnt out)
2. **Refer to attorney** — bankruptcy buys time + can sometimes save the house (best if they have equity + want to fight)
3. **Refer to loan-mod specialist** — modify the existing mortgage (best if income recovered)
4. **Refer to HUD counselor** — free government counseling (always offer, builds trust)

### Escalate to Aaron when

- Lead has property worth >$300K with significant equity
- Lead wants to sell + close within 21 days
- Lead has multiple properties (potential portfolio play)
- Lead asks "what's your offer?"

---

## §4-6. Sheriff Sale — Montgomery / Franklin / Greene

**Situation:** Sheriff sale is on the calendar. Days-to-sale dictates urgency. **Time pressure is the dominant force in every conversation.**

### Tone

Direct + urgent + pragmatic. Not panicked. Confidence: "I can close before the sale, here's how."

### Cadence (>21 days to sale)

| Day | Action | Channel | Template/Script |
|---|---|---|---|
| **Day 1** | Opening SMS — calm urgency | SMS | [§4/§5/§6 Day 1](SOP-SMS-TEMPLATES-BY-PRESET.md#4-sheriff-sale--montgomery) |
| **Day 2** | SMS follow-up | SMS | §4/§5/§6 Day 2 |
| **Day 3** | Phone call attempt #1 | Phone | SS phone opener (below) |
| **Day 5** | SMS check-in | SMS | "{{first}}, did you see my texts? {{address}} sale {{auction}} is coming. Quick chat?" |
| **Day 7** | Phone attempt #2 | Phone | Same opener, alternate voicemail |
| **Day 14** | Mailer #1 + phone attempt #3 | Phone + Mail | Mail: "{{auction}} sheriff sale — your options" |
| **Day 21** | SMS — explicit deadline reminder | SMS | "{{first}}, {{auction}} is now ~3 weeks out. After that the bank owns it. {{phone}} for options." |

### Cadence (<21 days to sale — URGENCY MODE)

When `auction_date - today < 21 days`:

| Day | Action | Channel | Template/Script |
|---|---|---|---|
| **Day 1** | Opening SMS — urgency variant | SMS | §4/§5/§6 Day 1 urgency variant |
| **Day 1 (PM)** | Phone call attempt #1 | Phone | Same-day call after SMS |
| **Day 2** | SMS + Phone | SMS + Phone | Both, different times of day |
| **Day 3** | Phone + voicemail | Phone | Urgency voicemail |
| **Day 5** | SMS — deadline math | SMS | "{{first}}, {{auction}} = X days. Last chance. {{phone}}." |
| **Day 7** | Final phone attempt + email if available | Phone + Email | "Pre-Sheriff-Sale Cash Offer" email |
| **Day until sale** | Daily SMS | SMS | "Day X before {{auction}}. Still time to act. {{phone}}." |

### SS Phone Opener (Day 3)

> "Hi {{first}}, this is {{me}} from Wright Home Offer. I'm calling because your property at {{address}} is set for sheriff sale on {{auction}}, and I wanted to make sure you know your options before that. Do you have 2 minutes? I'm not here to pressure — I just want to make sure you understand what happens after sale and what you can still do before."

### Solutions to offer in SS conversation

1. **Cash sale before sale** — most common path. We close in 7-10 days, payoff to bank, clean transaction.
2. **Negotiate with bank for short payoff** — if they're underwater, we can help structure
3. **Refer to attorney for emergency Chapter 13 filing** — last-resort but can stop the sale
4. **Help them prepare for redemption window** — if sale happens, they have OH redemption rights

### Escalate to Aaron when

- Sale is <14 days out + lead expresses any willingness
- Property has clear equity (Zestimate > likely payoff)
- Lead says "yes" to a meeting

---

## §7-9. Probate — Montgomery / Franklin / Greene

**Situation:** Talking to the **executor / personal representative** (named in court record). Their family member just died. They're grieving + dealing with a thousand admin tasks. **Empathy is non-negotiable.**

### Tone

Empathic. Soft. NEVER urgent on Day 1. We're patient — probate is a 6-9 month process.

### Cadence

| Day | Action | Channel | Template/Script |
|---|---|---|---|
| **Day 1** | Opening SMS — condolences first | SMS | [§7/§8/§9 Day 1](SOP-SMS-TEMPLATES-BY-PRESET.md#7-probate--montgomery) |
| **Day 3** | Soft follow-up | SMS | §7/§8/§9 Day 2 |
| **Day 7** | Nurture text — no pitch | SMS | "{{first}}, hope the week's been manageable. No pressure on {{address}} — just wanted to check in." |
| **Day 14** | First phone attempt | Phone | Probate phone opener (below) |
| **Day 30** | Mailer #1 — empathy-led | Mail | Mail: "Selling Estate Property — Your Options" |
| **Day 45** | Phone attempt #2 | Phone | Reference court timeline ("how's the estate process going?") |
| **Day 60** | SMS check-in | SMS | "{{first}}, things settling down? When ready to talk about {{address}}, I'm here." |
| **Day 90** | Phone attempt #3 + mailer #2 | Phone + Mail | "Probate Closing Soon — Final Options" |
| **Day 120** | Quarterly mailer schedule | Mail | Until probate closes |

### Probate Phone Opener (Day 14)

> "Hi {{first}}, this is {{me}}. I sent a couple texts a couple weeks back about {{decedent}}'s estate. I know dealing with everything after losing someone is overwhelming. I'm not calling to sell anything — I just wanted to introduce myself in case you ever want to talk through options on the property at {{address}}. Even if it's months from now, you have my number. Anything I can help with today?"

**Why this works:**
- "Not calling to sell anything" disarms the wall
- "Even if it's months from now" — respects their timeline
- "Anything I can help with today?" — opens space for THEM to lead

### Solutions to offer in Probate conversation

1. **Cash sale (most common)** — estate gets clean cash to distribute among heirs
2. **Refer to probate attorney** — if they don't have one (we know reliable ones)
3. **Help with property cleanout** — even if they don't sell, we have contractors who can clear estate items
4. **Wait for them** — sometimes the right answer is "I'll check back in 6 months"

### When NOT to push a probate lead

- They mention recent funeral (within 2 weeks)
- They say "we're still figuring things out" — back off, recycle in 30 days
- Multiple heirs in conflict — flag to Aaron for negotiation handling

### Escalate to Aaron when

- PR has Letters of Authority issued + says "yes, I want to sell"
- Property is vacant + costs money to maintain
- Multiple heirs willing to sell collectively
- Property has high value (>$300K) — Aaron handles direct

---

## §10. Redemption Window (post-sheriff-sale)

**Situation:** Sheriff sale already happened. In Ohio, the homeowner has a **redemption period** (varies — typically 0-180 days depending on case type) where they can buy the property back from the sale buyer. **Most don't realize this is possible.** Our angle: educate + offer to fund the redemption.

### Tone

Educational + helpful. They feel like they've already lost. We're the messenger of "wait, you might still have a path."

### Cadence

| Day | Action | Channel | Template/Script |
|---|---|---|---|
| **Day 1** (post-sale) | Opening SMS — inform of redemption rights | SMS | [§10 Day 1](SOP-SMS-TEMPLATES-BY-PRESET.md#10-redemption-window-post-sheriff-sale) |
| **Day 2** | Phone call attempt #1 — pure educational | Phone | RW phone opener (below) |
| **Day 4** | SMS — share resource link | SMS | "{{first}}, here's a quick guide on Ohio redemption rights: [link]. {{phone}} if questions." |
| **Day 7** | Phone attempt #2 | Phone | Reference any prior contact |
| **Day 10** | SMS — deadline math | SMS | "{{first}}, your redemption window has ~X days left on {{address}}. Want to talk options before it expires?" |
| **Day until redemption expires** | Daily SMS | SMS | "{{first}}, X days left to act on {{address}}. {{phone}}." |

### RW Phone Opener (Day 2)

> "Hi {{first}}, this is {{me}}. I know {{address}} sold at sheriff sale on {{auction}}. I wanted to call because most people in your situation don't realize Ohio gives you a redemption period — meaning you can still buy the property back from the new owner OR sell your redemption rights. I have cash available and I can structure a deal where you walk away with money instead of nothing. Got 5 minutes?"

**Key phrases:**
- "Most people don't realize" — they feel informed, not pitched
- "Walk away with money instead of nothing" — concrete benefit
- "I have cash available" — establishes capability fast

### Solutions to offer in RW conversation

1. **Fund the redemption + buy** — we provide cash to redeem (typically sale price + fees), then buy from them at agreed price. They walk with $5-30K.
2. **Sell their redemption rights** — even simpler, they assign rights to us, we redeem ourselves. Fast cash for them.
3. **Refer to bankruptcy attorney** — Chapter 13 can sometimes extend redemption / restructure debt
4. **Confirm timeline** — many don't know exactly when their window expires; we help them find out

### Critical RW timing notes

- **Tax foreclosure redemption (ORC 5721)** — redemption is allowed up to confirmation of sale (~30 days post-sale)
- **Mortgage foreclosure redemption** — redemption rights end at sale confirmation (typically 7-30 days post-sale)
- **Always verify exact deadline** before structuring offer — court docket or attorney confirms

### Escalate to Aaron immediately when

- Lead says "yes, help me redeem"
- Redemption window <14 days
- Property has clear equity above sale price (always — that's free money structure)

---

## What does Mike's typical day look like?

**Morning (9:00-11:00 AM):**
- Triage the new daily batch from autonomous run
- Apply each preset filter, work through Day 1 outreach
- Prioritize: Sheriff Sale <14 days first, then LP, then Probate, then RW

**Midday (11:00 AM-1:00 PM):**
- Phone call windows: respond to morning SMS replies
- Day 2/3 follow-ups for yesterday's Day 1 batch
- Update status tags as conversations progress

**Afternoon (1:00-4:00 PM):**
- Phone calls for Day 4/7/14 records
- Tag responders, escalate hot leads to Aaron
- Update DataSift status for moved leads

**End of day (4:00-5:00 PM):**
- Apply Sold Property Cleanup if any deals closed
- Schedule callbacks for tomorrow
- Friday only: Weekly Review per [SOP-WEEKLY-REVIEW.md](SOP-WEEKLY-REVIEW.md)

---

## Sequence priority order (when Mike's behind)

If Mike has 50 records to work and only 2 hours, work in this order:

```
1. Sheriff Sale <14 days to auction (highest urgency)
2. Redemption Window <14 days remaining (next-highest urgency)
3. Sheriff Sale 14-30 days to auction
4. Lis Pendens Day 1 (first-to-market window)
5. Probate Day 1 (less urgent but compounds — empathy demands prompt response)
6. Day 2-3 follow-ups (in order: SS → LP → Probate)
7. Day 7+ nurture cadence
```

---

## "Options-to-help" snippets Mike can drop into any conversation

Save these as DataSift quick-replies / phone-script snippets:

**Open with:**
> "I'm a local buyer in {{county}} County. Based on where your situation is now, I usually have 3-4 options to share — not all require selling. What's worth knowing for you?"

**If they push back:**
> "Totally fair. Just so you know what's on the table: I can buy as-is in 7 days, OR refer you to attorneys who can buy you time, OR help you understand your rights. I don't make money unless option 1 happens, but I share all of them anyway. What sounds useful?"

**If they're not ready:**
> "No problem. Drop my number — {{phone}} — and reach out if anything changes. I'm not going anywhere."

**If they say "send info":**
> "Will do. Texting you a one-page summary right now. If anything in there raises questions, just reply to this thread. Talk soon."

---

## Tags Mike applies as he works

| Tag | When to apply |
|---|---|
| `sms_sent_day1` | Right after Day 1 SMS goes out (auto-applied if using DataSift bulk SMS) |
| `sms_sent_day2`, `day3` | After each follow-up SMS |
| `called_day1`, `day2`, etc. | After each phone attempt (even VM) |
| `mailed_day14` | After Day 14 mail piece sent |
| `mailed_day60` | After Day 60 mail piece sent |
| `responded_v1` / `responded_v2` | A/B test variants — track which copy converts |
| `requested_info` | Lead asked for info — Mike sent summary |
| `callback_scheduled` | Lead booked a callback — set the date in DataSift calendar field |
| `interested` | Lead engaged positively, mid-conversation |
| `hot` | Ready to make offer — escalate to Aaron same-day |
| `not_interested` | Hard no — recycle in 90 days |
| `do_not_contact` | STOP reply or explicit ask not to contact |
| `bad_data` | Wrong number, wrong address — don't waste another attempt |

These tags drive the next-day preset filters, so consistency matters.

---

## When to graduate a lead OUT of the cadence

Move to Aaron / closer:
- Tag `hot` → same-day Slack ping to Aaron
- Status changes to `Appointment` → Aaron takes over

Drop the cadence:
- Tag `do_not_contact` → never touch again
- Status `Sold` → Sold Property Cleanup auto-fires
- 90-day recycle complete with no engagement → tag `dead_archive`, drop

Long-term nurture:
- Tag `not_interested` + meaningful reason → quarterly mailer, no SMS/calls
- Tag `requested_info` + 30 days no follow-up → quarterly mailer

---

## See also

- [SOP-SMS-TEMPLATES-BY-PRESET.md](SOP-SMS-TEMPLATES-BY-PRESET.md) — the actual template library
- [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) — phone call openers + objection handling
- [SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md) — 4 Pillars scoring + escalation
- [SOP-DAILY-OPERATIONS.md](SOP-DAILY-OPERATIONS.md) — Mike's morning playbook
- [SOP-DATASIFT-NAVIGATION.md](SOP-DATASIFT-NAVIGATION.md) — UI walkthrough for tagging + filtering
