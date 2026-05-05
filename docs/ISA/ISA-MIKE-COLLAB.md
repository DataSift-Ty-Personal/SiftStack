# Mike + Kareem Daily Collaboration Protocol

**Audience:** Mike (SMS channel), Kareem (phone channel), Aaron (oversight).
**Purpose:** The two of them work the same records on different channels. Without a tight handshake, they will step on each other's calls, miss handoffs, and confuse prospects. This doc is the structured daily dialogue Aaron wants visibility into.

---

## The mental model

Mike runs the **SMS channel** (200+ texts/day, mostly automated through DataSift sequences with manual responses). Kareem runs the **phone channel** (100-120 dials/day across FTM + bulk Top-250).

They're working the **same records** in DataSift but on different channels. Coordination happens through three things:

1. **DataSift tags** — the source of truth for who has done what to which record
2. **Group text dialogue** — the team thread, not a separate Mike-Kareem channel
3. **Daily live touchpoints** — three structured moments per day where they actually talk

---

## The three daily touchpoints (Aaron sees all three)

### Touchpoint 1 — Morning Standup (group text, 5 min, async)

**When:** Tue–Fri by 9:15 AM ET. Monday by 12:15 PM (Kareem's first hour).
**Who posts first:** Mike. Kareem replies.
**What goes in:**

**Mike's morning post (template):**
```
☀ MORNING — [date]
- New records overnight: [N FTM] + [bulk count if Top-250 refreshed]
- SMS responses overnight to triage: [N]
- Hot SMS responders worth a Kareem dial today: [list 1-3 with addresses]
- Any DNC / STOP / opt-outs to flag: [list]
- Anything Kareem should NOT call today (Mike mid-conversation): [list]
```

**Kareem's reply (template):**
```
Acknowledged. Today's plan:
- FTM Block #1: top 2 per preset, plus your hot SMS responders [verify list]
- Bulk Top-250 Block: [Franklin / Montgomery / Greene — pick the day's county]
- Callbacks scheduled: [N]
- Targeting [N] handoffs to Danielle today
```

**Aaron sees:** the dialogue happens on the team group text — Aaron reads it passively. If either of them skips the post, Aaron pings.

### Touchpoint 2 — Mid-day Sync (group text, 2 min)

**When:** Tue–Fri 1:00 PM. Monday 3:00 PM (mid-shift for Kareem). Skipped on Friday (replaced by retro).
**Purpose:** real-time deconfliction — who's mid-conversation with whom, what objections are coming up across both channels, are any patterns emerging.

**Either of them posts:**
```
☼ MID-DAY [date]
- Mike: connected with [N] via SMS this AM — biggest theme: [pattern]
- Kareem: dialed [N], connected [N], hit [N objection X times]
- Anyone we should NOT touch this afternoon: [list]
- Hot lead I'm working: [property + status] (so the other doesn't accidentally re-engage)
```

### Touchpoint 3 — End-of-Day Recap (group text, 5 min)

**When:** End of Kareem's shift (5:30-6:00 Tue-Thu, 4:30 Fri, 12:00 Sat).
**Who posts first:** Kareem (he's the closer of the day). Mike confirms.

**Kareem's EOD post (template — uses the same template as in [ISA-DAILY-PLAYBOOK.md](ISA-DAILY-PLAYBOOK.md)):**
```
KAREEM EOD — [date]
Dials: [N] | Connects: [N] | UMBC captures: [N]
Handoffs to Danielle: [N — list addresses]
Standout call: [1-2 sentences]
Stuck on: [optional]
Tomorrow's top priority: [...]
```

**Mike's confirm + add:**
```
MIKE EOD — [date]
SMS sent: [N] | Responses worked: [N]
Hot responders queued for tomorrow: [list]
Records flipped to Mike-pause (don't call): [list]
Records flipped to Mike-done (Kareem can call): [list]
```

**Aaron's daily read:** scans both, notes patterns over the week, flags issues at Friday retro.

---

## DataSift tag handshake — who does what to which record

This is the structural piece. The group text is fast coordination; the tags are the durable record.

| Tag | Who applies it | Meaning |
|---|---|---|
| `mike_active_sms` | Mike | Mike is mid-conversation via SMS — Kareem **does not call** this record |
| `mike_paused` | Mike | Mike sent something and is waiting for response — Kareem **can call** to add a voice layer |
| `mike_done` | Mike | Mike has stopped working this record (no response after N attempts, or he's punted to Kareem) |
| `kareem_active_call` | Kareem | Kareem is mid-conversation / has a callback scheduled — Mike **does not text** this record |
| `kareem_left_vm` | Kareem | Kareem left a voicemail — Mike can send a "saw my colleague Kareem reached out, here's a different angle" SMS as a layer |
| `kareem_done` | Kareem | Kareem has exhausted dials for this record — Mike resumes SMS-only |
| `joint_hot` | Either | Both Mike and Kareem think this lead is moving — collaborate live in group text |
| `pull_back` | Either | Stop everything on this record (DNC, prospect angry, attorney involved). Tagged immediately, both stop. |

**Rule:** before either of them touches a record, **check the tags first.** If `mike_active_sms` is on it, Kareem skips and works the next record. If `kareem_active_call` is on it, Mike does not send SMS. **No exceptions** — overlapping touches are how prospects get burned.

---

## Shared queue ownership

| Record type | Primary owner | Secondary support |
|---|---|---|
| **FTM Probate Day 1-7** | Kareem (calls) | Mike (SMS only after Kareem's first connect or VM) |
| **FTM Sheriff Sale ≤14 days** | Kareem (urgency) | Mike (parallel SMS, max velocity) |
| **FTM Sheriff Sale >14 days** | Co-owned | Mike opens Day 1 SMS; Kareem dials Day 1 PM |
| **FTM Lis Pendens** | Co-owned | Same — Mike Day 1 AM, Kareem Day 1 PM |
| **FTM Redemption Window** | Kareem (urgency) | Mike parallel |
| **Bulk Top-250** | Kareem (volume) | Mike opportunistically — sends SMS to Kareem's connects who didn't pick up |

The **co-owned** records are where the daily handshake matters most. Mike's morning post says "I sent SMS to records X, Y, Z this AM"; Kareem's reply says "got it, I'll dial X this morning, Y this afternoon, Z tomorrow." That's the deconfliction.

---

## Joint calls — when Mike + Kareem work a lead together

Sometimes a prospect responds to Mike's SMS asking a specific question Mike can't answer well over text ("how does the redemption process actually work?"). The cleanest move is for **Mike to hand the conversation to Kareem live** rather than try to text-explain.

**Mike's hand-off SMS to the prospect:**
> "Great question — my colleague Kareem can walk you through that on a quick call (he handles the foreclosure-side stuff). He's free now — okay if he calls you in the next 5 minutes? Or what's a better time?"

**Mike then group-texts Kareem:** "[address] — [first_name] wants the redemption process explained. Texting them you'll call in 5. Phone is [number]."

**Kareem dials within the promised window.** This is one of the highest-conversion paths Mike + Kareem have — the prospect is already warm from the SMS and now hears a calm voice. Aaron wants to see this happen at least 1-2x/day once they're in cruise pace.

---

## Practical collaboration during onboarding (Week 1-2)

In Kareem's first two weeks, Mike's role is partly **co-trainer**. Specific shared activities (also in [ISA-ONBOARDING.md](ISA-ONBOARDING.md)):

- **Day 2:** Mike sits in (or listens via Smrtphone live-listen if remote) on Kareem's first 5-10 calls, gives feedback after each.
- **Day 3-5:** Daily 15-min debrief between Mike and Kareem, end of day, before Kareem's EOD post. What worked, what jammed up.
- **Week 2:** Roleplay sessions Mike + Kareem run together — Mike plays prospect, Kareem reads scripts. Specific scenarios in [ISA-ONBOARDING-WORKBOOK.md](ISA-ONBOARDING-WORKBOOK.md).
- **Week 2 onward:** Tag-handshake audit — Mike pulls 10 records each Friday, checks whether Kareem applied tags correctly. Reports to Aaron at Friday retro.

---

## What Aaron is watching for in the dialogue

Aaron reads the group text passively every day. Specific things he flags:

- **Missing morning post** — either Mike or Kareem skipped → Aaron pings
- **Mid-day sync skipped 2+ days in a row** — pattern → Aaron addresses at Friday retro
- **EOD post missing handoff details** — quality issue → Aaron addresses next day
- **Tag mismatches** (Kareem dialed a record Mike had `mike_active_sms` on) — process violation → same-day correction
- **Repeated objections coming up across both channels** — signals script needs tuning → Aaron updates [ISA-CALL-SCRIPTS.md](ISA-CALL-SCRIPTS.md)
- **Patterns Mike + Kareem flag together** ("everyone in Montgomery is asking about X right now") — feeds into weekly intel + Aaron decides whether to adjust SOP

---

## What Mike + Kareem are NOT for

- **Texting the prospect at the same time as a call.** One channel at a time per record. Tags enforce this.
- **Disagreeing about a lead in front of the prospect.** If one of them thinks a lead is hot and the other thinks it's dead, they hash it out in group text first, then either one engages.
- **Replacing each other.** Mike does NOT make outbound dials (his role is SMS). Kareem does NOT manage SMS sequences (his role is calls). They support each other, they don't substitute.

---

## Escalation when collaboration breaks

| Situation | Action |
|---|---|
| Mike + Kareem disagree on a lead's status | Both flag in group text, Aaron makes the call within 4 hours |
| Tag mismatch causes a duplicate touch | Pull-back tag immediately, apologize to prospect on next channel, document in EOD post, retro at Friday |
| Mike or Kareem repeatedly skips a touchpoint | Aaron 1:1 with whoever skipped, before it becomes a habit |
| Prospect complains about overlapping outreach | Pull-back tag, same-day apology SMS from Mike, log incident, retro at Friday |

---

## See also

- [ISA-ROLE.md](ISA-ROLE.md) — what Kareem owns vs. doesn't
- [ISA-CADENCE.md](ISA-CADENCE.md) — the dial cadence Mike's SMS layers around
- [ISA-DAILY-PLAYBOOK.md](ISA-DAILY-PLAYBOOK.md) — Kareem's hour-by-hour day shape
- [ISA-ONBOARDING.md](ISA-ONBOARDING.md) — the Week 1-4 ramp where Mike co-trains
- [SOP-TAG-FLOW.md](../SOP-TAG-FLOW.md) — Mike's full DataSift tag taxonomy
- [SOP-DAILY-OPERATIONS.md](../SOP-DAILY-OPERATIONS.md) — Mike's full daily playbook
