# SOP — SMS Templates Mapped to Mike's Presets

**Audience:** Mike — daily text outreach.
**Purpose:** One template library covering every FTM preset Mike works. Each preset gets Day 1 / Day 2 / Day 3 cadence + an A/B variant for testing. Copy-paste into DataSift's SMS sender, swap variables, send.

---

## Quick reference — which template for which preset

| Mike opens preset | Use templates from section |
|---|---|
| `1. FTM_LP_Mont` | [§1 Lis Pendens — Montgomery](#1-lis-pendens--montgomery) |
| `2. FTM_LP_Franklin` | [§2 Lis Pendens — Franklin](#2-lis-pendens--franklin) |
| `3. FTM_LP_Greene` | [§3 Lis Pendens — Greene](#3-lis-pendens--greene) |
| `4. FTM_SS_Mont` | [§4 Sheriff Sale — Montgomery](#4-sheriff-sale--montgomery) |
| `5. FTM_SS_Franklin` | [§5 Sheriff Sale — Franklin](#5-sheriff-sale--franklin) |
| `6. FTM_SS_Greene` | [§6 Sheriff Sale — Greene](#6-sheriff-sale--greene) |
| `7. FTM_Probate_Mont` | [§7 Probate — Montgomery](#7-probate--montgomery) |
| `8. FTM_Probate_Franklin` | [§8 Probate — Franklin](#8-probate--franklin) |
| `9. FTM_Probate_Greene` | [§9 Probate — Greene](#9-probate--greene) |
| `000. FTM_RW` | [§10 Redemption Window](#10-redemption-window-post-sheriff-sale) |

---

## Variables you'll substitute

| Placeholder | DataSift field | Example |
|---|---|---|
| `{{first}}` | Owner First Name | "Jonathan" |
| `{{address}}` | Property Street Address | "2750 Winton Dr" |
| `{{auction}}` | Foreclosure Date / Tax Auction Date | "5/15/2026" |
| `{{decedent}}` | Decedent Name | "Patricia Cridge" |
| `{{relationship}}` | DM Relationship | "executor" |
| `{{me}}` | Your name (Aaron / Mike) | "Aaron" |
| `{{phone}}` | Your callback number | "(513) 555-0142" |

DataSift's SMS bulk-send replaces these from each record's mapped fields. Make sure the column mapping is set before you send a batch.

---

## Universal compliance footer

**Every Day-1 SMS must end with opt-out language** (TCPA requirement). Save this as a snippet:

```
Reply STOP to opt out.
```

For Day 2 and Day 3, opt-out is optional but recommended. After STOP, DataSift auto-flags the record so you don't text again.

---

## Length notes

- Single SMS = 160 chars. Going over splits into multi-part (more $$).
- Templates below are tagged `[160]` (single-segment) or `[multi]`.
- For Tier 1 phones, longer = fine. For Tier 3-4, keep to single segment to maximize delivery rate.

---

# 1. Lis Pendens — Montgomery

**Context:** Foreclosure case JUST filed in Common Pleas. Sheriff sale is 4-12 weeks away. Homeowner has more time + more options than sheriff sale stage. Highest motivation segment because they're in early panic mode.

### Day 1 (Tier 0-1) — Empathy + early option [160]

```
Hi {{first}}, this is {{me}} - local cash buyer in Dayton. Saw the foreclosure filing on {{address}}. Before this drags on, want options to walk away with cash? Reply STOP to opt out.
```

### Day 1 — A/B variant: Direct [160]

```
{{first}}, I'm a local Montgomery County buyer. Your case on {{address}} was just filed - I can buy fast and stop the foreclosure before sheriff sale. Open to a chat? STOP to opt out.
```

### Day 2 — Following up [160]

```
{{first}} - circling back on {{address}}. Even if you're talking to the bank, second opinions are free. We've helped 30+ Dayton homeowners avoid sheriff sale. Reply if interested.
```

### Day 3 — Final / urgency [160]

```
{{first}}, last note on {{address}}. Sheriff sale's coming. Selling to me before then keeps the foreclosure off your credit + you walk away with cash. {{phone}}. STOP to opt out.
```

---

# 2. Lis Pendens — Franklin

**Context:** Same as Montgomery LP but Columbus market. Higher home values, more competition from other investors. Lead with local presence to differentiate.

### Day 1 (Tier 0-1) [160]

```
Hi {{first}}, {{me}} here - I buy houses in Columbus. Your case on {{address}} was just filed and I wanted to reach out before the sheriff sale. Open to options? STOP to opt out.
```

### Day 1 — A/B variant: Equity angle [multi]

```
{{first}} - I'm a local Columbus buyer. I noticed the foreclosure case on {{address}}. Most homeowners in your spot have equity left - I can help you keep it instead of losing it at auction. Worth a quick chat? Reply STOP to opt out.
```

### Day 2 [160]

```
{{first}}, did you see my text yesterday? On {{address}} - Franklin County sheriff sales typically run 60-90 days from filing. We can close before that. Reply for details.
```

### Day 3 — Final [160]

```
Last reach-out, {{first}}. {{address}} - I'm a real local buyer, not a national company. Quick close, all-cash, you avoid the foreclosure. {{phone}}. STOP to opt out.
```

---

# 3. Lis Pendens — Greene

**Context:** Smaller market, more personal. Greene LP is currently sentinel-mode (license disabled at source) so when this preset starts populating, it's special.

### Day 1 [160]

```
Hi {{first}}, {{me}} here from Greene County. Saw the case filed on {{address}}. Before things move further, I'd like to make a fair cash offer to help you avoid foreclosure. STOP to opt out.
```

### Day 1 — A/B variant: Hometown angle [160]

```
{{first}} - I live and buy in Greene County. Your case on {{address}} just got filed. Local, fast, fair. Want to chat about options before sheriff sale? STOP to opt out.
```

### Day 2 [160]

```
{{first}}, following up on {{address}}. I know dealing with foreclosure stuff is overwhelming. Even if you're handling it, my number stays in your back pocket: {{phone}}.
```

### Day 3 [160]

```
{{first}}, last message on {{address}}. Greene County sheriff sales are scheduled fast once cases mature. Selling to me first means cash + clean credit. {{phone}}. STOP to opt out.
```

---

# 4. Sheriff Sale — Montgomery

**Context:** Property has confirmed sheriff sale on `{{auction}}`. Days until sale = urgency multiplier. Check `auction_date` before sending — if <14 days, use urgency variant.

### Day 1 (>21 days to sale) [160]

```
Hi {{first}}, {{me}} - local Dayton buyer. {{address}} is set for sheriff sale {{auction}}. We can close before then so you walk away with cash. Reply or call {{phone}}. STOP opts out.
```

### Day 1 — Urgency variant (<14 days to sale) [160]

```
{{first}} - sheriff sale on {{address}} is {{auction}}. That's close. I'm a cash buyer in Dayton who can close in 7 days, stop the auction, save your credit. {{phone}}. STOP to opt out.
```

### Day 2 [160]

```
{{first}}, sheriff sale {{auction}} is approaching for {{address}}. Even if the bank's working with you, a foreclosure on your record sticks. Selling to me before sale clears it.
```

### Day 3 — Final [160]

```
Last try, {{first}}. {{address}} sells {{auction}}. After that, your options narrow to redemption period only. Call/text {{phone}} - Aaron buys all-cash, fast.
```

---

# 5. Sheriff Sale — Franklin

**Context:** Columbus sheriff sales fire weekly via RealAuction. Higher property values = more skeptical owners (think they have leverage). Lead with comparable closes / track record.

### Day 1 [160]

```
Hi {{first}}, {{me}} - I buy houses in Columbus. {{address}} is up for sheriff sale {{auction}}. Cash close, no agent fees, no inspection. Walk away with money. STOP to opt out.
```

### Day 1 — A/B variant: Track record [multi]

```
{{first}} - your sheriff sale on {{address}} is {{auction}}. I've helped 22 Franklin County homeowners avoid sheriff sale this year alone. Quick close, you keep your equity, foreclosure off your record. Reply for details. STOP to opt out.
```

### Day 2 [160]

```
{{first}}, on {{address}} - {{auction}} is coming up fast. Most homeowners think the bank will work it out. They almost never do. I can close before then. {{phone}}.
```

### Day 3 [160]

```
{{first}}, sheriff sale {{auction}} is days away. After that, the bank owns it and YOU are out. Selling to me now = you keep cash. {{phone}}. STOP to opt out.
```

---

# 6. Sheriff Sale — Greene

**Context:** Greene SS volume is low — maybe 1-3 per month. When it fires, treat each lead as high-priority because there's less haystack.

### Day 1 [160]

```
Hi {{first}}, this is {{me}}, a Greene County local. Sheriff sale on {{address}} is {{auction}}. I'd like the chance to make a cash offer before then. Reply or call {{phone}}. STOP to opt out.
```

### Day 1 — A/B variant: Personal note [160]

```
{{first}} - sheriff sale {{auction}} for {{address}} is coming up. I'm right here in Greene County and I close fast. No banks, no agents, no surprises. {{phone}}. STOP to opt out.
```

### Day 2 [160]

```
{{first}}, following up on {{address}}. Sale {{auction}}. The clock matters - even a few days head start lets us close cleanly. Reply if you want to talk.
```

### Day 3 [160]

```
{{first}}, last note before {{auction}}. Greene County sheriff sales close fast - your options narrow significantly after. {{phone}}. Reply STOP to opt out.
```

---

# 7. Probate — Montgomery

**Context:** Talking to the **executor**, not the deceased. Their family member just died. Lead with empathy. Do NOT push hard on Day 1.

### Day 1 (Tier 0-1) — Empathy first [multi]

```
Hi {{first}}, this is {{me}} - I buy houses in Dayton. I saw you were named {{relationship}} on {{decedent}}'s estate. First, my condolences. I noticed there's a property at {{address}} - if you ever want to sell quickly without dealing with showings, I'm here. STOP to opt out.
```

### Day 1 — A/B variant: Brief + soft [160]

```
{{first}}, condolences on {{decedent}}. I'm a local Dayton buyer. If selling {{address}} would help close out the estate, I can make a cash offer. No pressure. STOP to opt out.
```

### Day 2 [160]

```
{{first}}, hope you're doing OK. On {{address}} - I close in 14 days, all-cash, no repairs needed. The estate gets clean money fast. Worth chatting? Reply yes/no.
```

### Day 3 [160]

```
Last note, {{first}}. {{address}} - probate properties cost the estate every month they sit. I take it as-is, your family moves on. {{phone}}. STOP to opt out.
```

---

# 8. Probate — Franklin

**Context:** Columbus probate volume is highest of the 3 counties. Many records will have heir maps with multiple decision-makers. Mike should still text the named executor first.

### Day 1 — Empathy first [multi]

```
Hi {{first}} - {{me}} here, local Columbus buyer. I saw you're {{relationship}} on {{decedent}}'s estate. Sorry for your loss. If selling {{address}} would help close things out, I can make a fair cash offer. Just reply if open to it. STOP to opt out.
```

### Day 1 — A/B variant: Multi-heir aware [multi]

```
{{first}}, condolences on {{decedent}}. As {{relationship}}, I know you're juggling a lot. On {{address}} - I close fast, your siblings/heirs get clean cash to split, no fights over showings. {{phone}}. STOP to opt out.
```

### Day 2 [160]

```
{{first}}, on {{address}} - I know probate moves slow. I can move fast. All-cash, 14-day close, your family moves on. Reply if interested.
```

### Day 3 [160]

```
{{first}}, last message about {{address}}. Probate property carrying costs (taxes, insurance, lawn) eat into the estate. Selling now = more for the heirs. {{phone}}.
```

---

# 9. Probate — Greene

**Context:** Greene probate is currently sentinel-mode (license disabled at source). When this fires, it's gold — competitors have no idea. Be tasteful.

### Day 1 — Empathy + small-town tone [multi]

```
Hi {{first}}, this is {{me}}. I'm sorry to reach out at a hard time - I saw you're {{relationship}} on {{decedent}}'s estate. I'm a Greene County local buyer. If selling {{address}} would help, I'd love to chat. STOP to opt out.
```

### Day 1 — A/B variant [160]

```
{{first}}, my condolences on {{decedent}}. Local Greene County buyer here - if {{address}} needs to be sold quickly, I can help. {{phone}}. STOP to opt out.
```

### Day 2 [160]

```
{{first}}, hope you're holding up. On {{address}} - if the family wants to move it fast and split clean cash, I'm here. Reply yes/no.
```

### Day 3 [160]

```
{{first}}, last note on {{address}}. Greene properties don't get the attention Columbus or Dayton ones do - I can give you a fair offer fast. {{phone}}.
```

---

# 10. Redemption Window (post-sheriff-sale)

**Context:** Sheriff sale already happened. In Ohio, homeowner has redemption period (typically days-to-weeks) to buy property back. Different play: we negotiate WITH the redeeming homeowner OR offer to fund the redemption + buy from them.

### Day 1 — Inform + offer help [multi]

```
Hi {{first}}, {{me}} here. I know {{address}} sold at sheriff sale recently. You may still have redemption rights. I can fund the redemption AND buy from you - you walk away with cash instead of nothing. STOP to opt out.
```

### Day 1 — A/B variant: Direct [160]

```
{{first}} - sheriff sale on {{address}} happened, but you may have redemption rights left. I have cash ready. Reply if you want to talk. STOP to opt out.
```

### Day 2 [160]

```
{{first}}, on {{address}} - the redemption window in Ohio is shorter than people think. If you want to act, I can structure a deal where I fund redemption + you get cash. {{phone}}.
```

### Day 3 [160]

```
{{first}}, last message about {{address}}. Once redemption expires the property's gone for good. Acting now = cash in hand. {{phone}}. STOP to opt out.
```

---

## How Mike sends these in DataSift

1. **Records → Apply preset** (e.g., `7. FTM_Probate_Mont`)
2. **Filter to Tier 0-1 phones only** (skip Tier 4-5 for SMS Day 1)
3. **Select all matching**
4. **Send To → Bulk SMS** (or DataSift's integrated SMS tool)
5. **Paste the Day 1 template** for that preset
6. **Verify variable mapping** (DataSift fills `{{first}}` etc. from each record)
7. **Send**
8. **Tag records `sms_sent_day1`** so they auto-pick into "02. Needs Called Day 1" preset tomorrow

For Day 2 / Day 3 — same flow but use the Day 2 / Day 3 preset filter (records tagged `sms_sent_day1` → "Day 2" cadence).

---

## A/B testing framework

Each preset has a primary Day 1 + A/B variant. Mike should:

- **Week 1:** send primary template to 50% of new records, A/B variant to other 50%. DataSift's bulk SMS supports randomized template assignment.
- **Week 2:** track response rate per variant.
- **Week 3:** standardize on the better-performing variant. Iterate the loser.

Track in DataSift: tag responders `responded_v1` or `responded_v2`. Friday Weekly Review pulls the conversion delta.

---

## Compliance reminders

- **TCPA:** every Day 1 SMS includes "Reply STOP to opt out". Day 2-3 optional but encouraged.
- **Time-of-day:** OH state law restricts SMS to 8 AM - 9 PM local. DataSift's SMS scheduler should respect this.
- **Frequency:** max 1 SMS/day per record. Don't double-text.
- **STOP handling:** DataSift auto-tags records that reply STOP. Never re-text those.
- **Litigator risk (Tier 5):** records with phone tier ≤ 20 are flagged litigator-risk. **Do not text Tier 5.** Mail or drop only.

---

## Customization tips

- **Personalize the city in templates** (Dayton / Columbus / Xenia) — generic "your city" reads as spam
- **Drop the brand on Day 1** if you want — sometimes anonymous "{{me}} here" converts better than "{{me}} from Wright Home Offer"
- **Vary phone display** — `{{phone}}` formatted as `(513) 555-0142` reads more local than `5135550142`
- **Don't send the same template to records that previously responded "not interested"** — they remember

---

## Future templates (Phase 11+)

When these niches come online:

- **Tax Sale (FTM_TaxSale_*)** — when Mike adds those presets
- **Code Violation** — when we add the scrapers
- **Eviction (landlord targeting)** — different angle: target the LANDLORD (plaintiff), not tenant
- **Divorce** — sensitive; usually only post-decree property division

I'll add templates here when those scrapers + presets land.

---

## See also

- [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) — phone scripts for the call cadence
- [SOP-LEAD-QUALIFICATION.md](SOP-LEAD-QUALIFICATION.md) — how to score responders
- [SOP-DATASIFT-NAVIGATION.md](SOP-DATASIFT-NAVIGATION.md) — UI walkthrough for sending bulk SMS
- [SOP-DAILY-OPERATIONS.md](SOP-DAILY-OPERATIONS.md) — when to send what
