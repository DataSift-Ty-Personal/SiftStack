# SOP — Mail Templates & Cadences (OpenLetter)

**Audience:** Aaron (template owner), Mike (DataSift sequence operator), future ISA.
**Purpose:** Canonical library of direct-mail templates and cadences for each distress type. Loaded into OpenLetter (DataSift's native integrated mail house) so cadences fire automatically from DataSift sequences without CSV upload friction.
**Companion docs:** [SOP-DIRECT-MAIL-PLAN.md](SOP-DIRECT-MAIL-PLAN.md) (economics + ramp plan), [SOP-REDEMPTION-WINDOW.md](SOP-REDEMPTION-WINDOW.md) (full redemption niche operational guide), [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) (SMS + phone scripts that pair with the mail), [MIKE-MAIL-ENTRY-SETUP.md](MIKE-MAIL-ENTRY-SETUP.md) (build sheet for the new Mail_On_Entry sequence).

> **Active model (2026-04-29):** Entry-mail-first. Every new mailable record gets ONE first-touch piece on Day 0 via the global `Mail_On_Entry` sequence — captures the first-to-market window before nurture cadences kick in. Existing preset cadences (FTM_Probate_Cadence, FTM_SS_Cadence, etc.) handle Day 30 / 60 / 90+ follow-up. No preset sub-tiering required. See [MIKE-MAIL-ENTRY-SETUP.md](MIKE-MAIL-ENTRY-SETUP.md) for the build sheet.

> **Template status (2026-04-29):**
> - ✅ **Probate-1, Probate-2, Probate-3, Probate-4** — bodies finalized (below)
> - ✅ **RW-1** — body finalized (below)
> - ⏳ **SS-1, SS-2, SS-3** — Aaron is writing final foreclosure mail copy. Drafts below are placeholders to be replaced. Mike can build the `Mail_On_Entry` sequence with placeholder text and swap the body when final copy lands.
> - ⏳ **LP-1, LP-2, LP-3, LP-4** — Same status as SS templates. Drafts below are placeholders.

---

## Vendor & integration overview

**OpenLetter** (DataSift's native integrated mail house). Replaces the prior YellowLetters.com plan in [SOP-DIRECT-MAIL-PLAN.md](SOP-DIRECT-MAIL-PLAN.md). Why this is better:

- **No CSV export** — sequences trigger mail directly from DataSift records
- **Variable substitution** uses DataSift custom fields natively (Sheriff Sale Held Date, Confirmation Hearing Date, Decedent Name, etc.)
- **Tracking** flows back into DataSift activity (response calls, returned mail, opt-outs)
- **Cadence automation** — Day-N triggers fire from the sequence, no manual reupload
- **Per-piece A/B variants** can be tested within a single cadence

**Format choice:** Handwritten yellow legal-pad style letter for empathy-driven messages (probate, redemption). Standard letter (#10 envelope, blue ink) for urgency-driven foreclosure messages. Postcards reserved for late-stage urgency only — they signal "we're an investor" and break empathy.

**Template loading workflow** (Mike, ~10 min per template):
1. DataSift → Sequences → Edit existing or Create new
2. Add action: "Send Mail via OpenLetter"
3. Pick format (yellow letter / standard / postcard)
4. Paste template body from this doc into the OpenLetter editor
5. Map variables — OpenLetter's variable picker shows DataSift custom fields. Match against the variable reference table below.
6. Set cadence trigger (Day 0, Day 14, Day 28, etc. from sequence start)
7. Set targeting filter (which preset records get this cadence)
8. Save + activate

---

## Variable reference (DataSift custom field → template variable)

OpenLetter likely accepts `{{variable_name}}` mustache-style. **Verify exact syntax in OpenLetter's variable picker before loading.** Substitute the right field name if their format differs.

| Template variable | DataSift source field | Notes |
|---|---|---|
| `{{first_name}}` | Owner First Name | For deceased records, this is the Decision Maker's first name (DataSift formatter handles the swap upstream) |
| `{{last_name}}` | Owner Last Name | Same swap rule for deceased |
| `{{property_address}}` | Property Street Address + City + State + ZIP | Single-line concatenation; OpenLetter may have a `{{full_property_address}}` shortcut |
| `{{property_street}}` | Property Street Address | Just the street line (when you want compact reference) |
| `{{property_city}}` | Property City | "Columbus", "Dayton", "Xenia", etc. |
| `{{county}}` | County | "Franklin", "Montgomery", "Greene" |
| `{{decedent_name}}` | Decedent Name | Probate only |
| `{{relationship}}` | DM Relationship | Probate only — "executor", "administrator", "personal representative" |
| `{{auction_date}}` | Foreclosure Date OR Tax Auction Date | Format M/D/YYYY |
| `{{sale_held_date}}` | Sheriff Sale Held Date | Redemption only — date the auction happened |
| `{{confirmation_date}}` | Confirmation Hearing Date | Redemption only — when the court confirms (window closes) |
| `{{days_remaining}}` | Redemption Days Remaining | Redemption only — fall back to "a few" if blank |
| `{{your_name}}` | Hardcode "Aaron" | Sender first name |
| `{{your_number}}` | Hardcode Wright Home Offer number | Sender phone — keep consistent across all pieces for inbound tracking |
| `{{your_email}}` | Hardcode aaron@wrighthomeoffer.com | Sender email |

---

## How the cadence model works (entry mail + preset follow-up)

Every record gets a single first-touch piece on Day 0 via the global `Mail_On_Entry` sequence. After that, the preset-specific cadence (FTM_Probate_Cadence etc.) takes over for follow-up touches at Day 30 / 60 / 90+.

| Day | Source of mail step | What fires |
|---|---|---|
| 0 (record uploads) | `Mail_On_Entry` (global) | Probate-1 / SS-1 / LP-1 / RW-1 — branched on Notice Type |
| 1–14 | Preset cadence | SMS + ISA call (NO mail — Day 1 mail step gated by `mail_entry_sent` tag) |
| 30 | Preset cadence | Probate-2 / SS-2 / LP-2 |
| 60 | Preset cadence | Probate-3 / SS-3 / LP-3 |
| 90+ | Preset cadence | Quarterly nurture (Probate-4 / LP-4) |

The Day 1 mail step in each preset cadence has a `NOT mail_entry_sent` filter so it never double-sends. Records that somehow miss `Mail_On_Entry` (mailable=no, missing DPV, etc.) still get their Day 1 mail from the preset cadence as a backup.

---

## Probate cadence

**Targeting:** records in `FTM_Probate_*` preset (Franklin / Montgomery / Greene), `mailable=yes`, decision_maker_status=verified_living.

**Cadence:** 4 mail pieces over ~6 months, then quarterly nurture. Probate-1 fires on Day 0 via `Mail_On_Entry`; Probate-2/3/4 fire from the preset cadence sequence.

| Piece | Trigger | Format | Tone |
|---|---|---|---|
| **Probate-1** | Day 0 (record uploads — fired by `Mail_On_Entry`) | Yellow letter | Empathy + soft offer |
| **Probate-2** | Day 30 if status still New/Contacted (preset cadence) | Yellow letter | Practical — vacant property burden |
| **Probate-3** | Day 60 if no engagement (preset cadence) | Yellow letter | Final direct outreach |
| **Probate-4 (nurture)** | Day 120, then every 90 days (preset cadence) | Yellow letter | Standing offer; estates often take 6-18 months to resolve |

### Probate-1 (Day 0 — empathy + introduction)

```
{{date}}

Dear {{first_name}},

I hope this finds you well. My name is Aaron and I run a small
home-buying business here in {{county}} County, Ohio. I'm reaching
out because I saw the probate filing for {{decedent_name}} — first
off, my deepest condolences. I'm sorry for your loss.

I noticed the estate includes a property at {{property_address}}.
I know dealing with the home during probate is one more thing on
top of an already difficult time. If you'd ever consider selling
to a local cash buyer — no agent fees, no repairs needed, no
showings — I'd love the chance to make you a fair offer.

If now isn't the right time, I completely understand. Just keep
my number for whenever you're ready.

With respect,

Aaron Leddy
Wright Home Offer
{{your_number}}  |  {{your_email}}
```

### Probate-2 (Day 30 — practical burden)

```
{{date}}

{{first_name}},

Following up on my earlier note about {{decedent_name}}'s estate.
I know probate can drag on, and the property at {{property_address}}
is probably one more thing weighing on you — taxes, insurance,
maintenance, maybe a vacancy you're trying to manage.

I close in 14 days, pay all closing costs, and buy as-is — no
inspection contingencies, no agent commission, no clean-out. If
the estate just needs to be DONE, I might be your fastest path.

Whenever you're ready: {{your_number}}.

Aaron
Wright Home Offer
```

### Probate-3 (Day 60 — direct, final)

```
{{date}}

{{first_name}},

This is my third and final letter about the {{property_address}}
property. I don't want to be a bother — I just wanted to make sure
my offer was on your radar in case the estate is reaching the
point where you'd rather move on than keep managing it.

If selling to a cash buyer makes sense, I'd love a quick call. If
not, no hard feelings — I won't write again unless you reach out.

{{your_number}} or {{your_email}} — anytime.

Aaron Leddy
Wright Home Offer
```

### Probate-4+ (Day 120+ — nurture, every 90 days)

```
{{date}}

{{first_name}},

Just a quick note to let you know my offer on {{property_address}}
still stands. Probate timelines vary so much — sometimes families
are ready to sell at month 6, sometimes at month 18. Whenever
that's you, I'm here.

{{your_number}}.

Aaron
Wright Home Offer
```

---

## Foreclosure cadence

Foreclosure has TWO upstream stages — lis pendens (case filed, 4-12 weeks pre-sale) and auction listing (sale scheduled, weeks-out). Different urgency, different message angles. Records can enter at either stage depending on which scraper catches them first.

**Targeting (split by tag):**
- Records in `FTM_LP_*` preset (lis pendens, no auction date yet) → lis pendens cadence
- Records in `FTM_SS_*` preset (auction listing, has auction_date) → sheriff sale cadence

### Lis pendens cadence (4 pieces over 90 days)

For records caught at the Common Pleas Court filing stage. Long runway = nurture-style cadence with progressive urgency as the case moves toward sale.

> **Aaron is writing final body copy for LP-1 through LP-4.** Drafts below are placeholders. Mike: build the cadence structure and use placeholder bodies until Aaron sends final copy.

| Piece | Trigger | Format | Tone |
|---|---|---|---|
| **LP-1** | Day 0 (record uploads — fired by `Mail_On_Entry`) | Yellow letter | Acknowledge filing, options |
| **LP-2** | Day 30 if status New/Contacted (preset cadence) | Yellow letter | Mortgage modification / sale comparison |
| **LP-3** | Day 60 (preset cadence) | Standard letter | Concrete deal mechanics |
| **LP-4** | Day 90 if record still active and no auction date (preset cadence) | Standard letter | Final pre-auction outreach |

#### LP-1 (Day 0 — acknowledge, soft options)

```
{{date}}

Dear {{first_name}},

My name is Aaron with Wright Home Offer, a local cash buyer in
{{county}} County. I'm writing because the public court records
show a foreclosure case was just filed against your property at
{{property_address}}.

I'm not a debt collector or a lawyer — I'm a homeowner who buys
houses. I'm reaching out early because most foreclosures take
6-12 months to reach the sheriff sale, which means you have time
to figure out what's next on YOUR terms before the bank does it
to you.

If you want to talk through options — selling to me before things
escalate, working out a short sale, or just understanding what
the bank can and can't do — I'd be glad to help. Free, no pressure.

{{your_number}} or {{your_email}}.

Aaron Leddy
Wright Home Offer
```

#### LP-2 (Day 30 — comparison angle)

```
{{date}}

{{first_name}},

Following up on the foreclosure case at {{property_address}}.

I want to share something most people in your situation don't
know: there's a real difference between letting the bank take
the property at auction vs. selling to a cash buyer like me
before things escalate.

If the bank takes it: foreclosure on your credit for 7+ years,
deficiency judgment possible, no equity recovery.

If you sell to me before the sale: foreclosure can sometimes be
reported as "settled" instead of "foreclosed," you walk away
with cash if there's any equity, and you control the timeline.

Worth 10 minutes to compare? {{your_number}}.

Aaron
Wright Home Offer
```

#### LP-3 (Day 60 — concrete mechanics)

```
{{date}}

{{first_name}},

Quick update on my offer for {{property_address}}.

Here's how a sale to me works in practice:
  1. We talk for 10 minutes about the property + your loan balance
  2. I make a written offer the same day or next morning
  3. If you accept, we close in 7-14 days
  4. The bank gets paid off, you walk with whatever equity is
     left, the foreclosure case dismisses

No agent fees, no repairs, no inspection contingencies. I close
on my own — I'm not a wholesaler "shopping" the contract.

References from past Wright Home Offer sellers available on request.

{{your_number}}, anytime.

Aaron Leddy
Wright Home Offer
```

#### LP-4 (Day 90 — final pre-auction)

```
{{date}}

{{first_name}},

I've written a few times about the foreclosure on
{{property_address}}. The case has been active 90+ days now,
which usually means the sheriff sale is coming within the next
3-4 months.

If you're still trying to work it out with the bank — good, keep
going. If you're at the point where selling makes more sense
than continuing the fight, I'm still here. The earlier we talk,
the more options you have.

After the sheriff sale is scheduled, the timeline gets tight.

{{your_number}}.

Aaron
Wright Home Offer
```

### Sheriff sale cadence (3 pieces over 14-28 days)

For records caught at the auction-listing stage. Auction is 4-8 weeks out at first listing — tight but workable. Cadence compresses to days, not weeks.

> **Aaron is writing final body copy for SS-1 through SS-3.** Drafts below are placeholders. Mike: build the cadence structure and use placeholder bodies until Aaron sends final copy.

| Piece | Trigger | Format | Tone |
|---|---|---|---|
| **SS-1** | Day 0 (record uploads — fired by `Mail_On_Entry`) | Yellow letter | Sale notice acknowledge, options |
| **SS-2** | Day 7 if no engagement (preset cadence) | Standard letter | Concrete offer + close timeline |
| **SS-3** | 7 days before auction date (preset cadence) | Postcard or standard letter | Final urgency |

#### SS-1 (Day 0 — sale notice, options)

```
{{date}}

Dear {{first_name}},

I'm Aaron with Wright Home Offer, a local cash buyer in
{{county}} County. I'm reaching out because I saw your property
at {{property_address}} is scheduled for sheriff sale on
{{auction_date}}.

I want you to know there ARE still options. If you sell to me
before the sale, you walk away with cash AND keep the foreclosure
off your credit report. I close in 7-10 days, pay all closing
costs, and you don't lift a finger to fix anything.

If you've been told "nothing can stop the sale" — that's not
true. The sale stops the moment the bank gets paid off, and a
sale to me pays them off.

If that's interesting, call or text me at {{your_number}}.

Aaron Leddy
Wright Home Offer
```

#### SS-2 (Day 7 — concrete offer)

```
{{date}}

{{first_name}},

Following up on the sheriff sale at {{property_address}} on
{{auction_date}}.

Here's what a deal with me typically looks like for a property
in your situation:
  - Cash offer based on the property's condition + comps
  - 7-day close (we can move faster if needed)
  - Bank gets paid off, foreclosure case dismissed
  - You walk with cash for any equity above the loan
  - No agent fees, no repairs, no showings

If your loan balance is roughly known and you can give me 10
minutes on the phone, I can usually quote a number same-day.

{{your_number}}.

Aaron
Wright Home Offer
```

#### SS-3 (~7 days before auction — final urgency)

```
{{date}}

{{first_name}},

The sheriff sale on {{property_address}} is scheduled for
{{auction_date}} — about a week away.

After that date, the property goes to whoever wins at auction
(usually the bank). Your equity, if any, is gone. Foreclosure
goes on your credit as completed.

It's not too late to sell to me first. We can close before
{{auction_date}} if we start today. {{your_number}} — even just
to talk through whether it makes sense.

Aaron Leddy
Wright Home Offer
```

---

## Redemption window cadence

For records that already went to sheriff sale and are now in the post-auction redemption window (between sale and court confirmation, ORC §2329.33). Window is 7-30 days. **One mail piece, FedEx 2-day if `redemption_closing` (≤14 days to confirmation).** Standard mail is too slow for this segment.

**Targeting:** records in `FTM_RW_OH_Redemption_Open` or `FTM_RW_OH_Redemption_Closing` preset (per [SOP-REDEMPTION-WINDOW.md](SOP-REDEMPTION-WINDOW.md)).

| Piece | Trigger | Format | Tone | Shipping |
|---|---|---|---|---|
| **RW-1** | Day 0 (record enters FTM_RW preset, status=open) | Standard letter | Education + mechanism | Standard mail |
| **RW-1-RUSH** | Day 0 (record enters preset with status=closing OR ages into closing) | Standard letter | Same body, different envelope | **FedEx 2-day** |

### RW-1 / RW-1-RUSH (single piece — different shipping based on urgency)

The body is identical for both versions. Only the envelope/shipping differs. Mike sets up two cadence triggers in DataSift:
1. Trigger A: status=open AND days_remaining > 14 → fires standard mail
2. Trigger B: status=closing OR days_remaining ≤ 14 → fires FedEx 2-day

```
{{date}}

Dear {{first_name}},

I know the sheriff sale on {{sale_held_date}} was a hard moment.
I'm writing because in Ohio, you still have rights until the court
confirms the sale on {{confirmation_date}} — that gives us about
{{days_remaining}} days to act.

Here's what most people don't know: under Ohio law (ORC §2329.33),
the homeowner can redeem the property all the way until the court
enters an Order Confirming Sale. The auction did not end your
rights — confirmation does.

Most homeowners can't redeem because they don't have the cash to
pay off the lender. That's where I come in. I can pay off the
lender, the court costs, and the fees, and you walk away with
cash for whatever equity is left.

That's a legal redemption, recordable transaction, done by your
{{confirmation_date}} hearing. Done right, you keep the
foreclosure off your record AND walk with money instead of
nothing.

If there's any path here, please call me at {{your_number}}. The
window closes {{confirmation_date}} — after that, the property
is gone for good.

Aaron Leddy
Wright Home Offer
{{your_number}}  |  {{your_email}}
```

**Why no second piece:** the window is ≤30 days. A second piece sent at Day 14 wouldn't arrive until Day 17-19, leaving ≤11 days to transact. Better to spend that budget on a phone-call follow-up + door knock (Phase 2, pending partner alignment).

---

## Cross-cadence rules

Things that apply across all distress types:

### Stop-mail triggers

OpenLetter cadences MUST honor these globally:

- Status changes to `Sold`, `Closed`, `Dead`, `Under Contract`, `Offer`, `Appointment` → halt remaining cadence
- Tag `do_not_mail` applied → halt all current AND future mail to this record
- Tag `bankruptcy_stay` applied → halt mail (we don't compete against active stays)
- Tag `co_owner_blocked` applied → halt mail (deal is dead)
- Status `Interested` → ONE more touch allowed, then pause for ISA call to continue

### Sign-off conventions

- **Always sign as "Aaron Leddy" + "Wright Home Offer"** — never just "Wright Home Offer" (depersonalizes)
- **Always include phone + email** — multiple channels = higher response
- **Phone is the same number across all templates** — inbound tracking depends on consistency
- **No P.S. line** — yellow letters look more genuine without one. Postcards can have one.

### Personalization guards

OpenLetter should be configured to **skip mail and log error** if any of these substitutions are blank:
- `{{first_name}}` — generic salutation looks like spam
- `{{property_address}}` — entire piece loses credibility without the property
- `{{decedent_name}}` (probate only)
- `{{auction_date}}` (foreclosure SS only)
- `{{sale_held_date}}` + `{{confirmation_date}}` (redemption only)

Better to skip a piece than send "Dear , I'm reaching out about your property at ."

### Returned mail handling

- 1 returned piece → tag `mail_returned_1` (no action; sometimes addresses are temporarily off)
- 2 returned pieces → tag `mail_returned_2` + halt remaining cadence + flag to Mike for skip-trace re-run
- 3+ returned pieces → tag `do_not_mail` permanently

DataSift should track returns automatically once OpenLetter posts the return event back to the record's activity log.

### A/B variant testing (optional, Phase 2+)

When mail volume is steady (≥500 pieces/month per cadence), set up A/B variants on the highest-ROI piece in each cadence:

- **Probate-1:** test "I hope this finds you well" vs "I'm writing because" opener
- **SS-1:** test yellow letter vs standard letter format
- **RW-1:** test "redemption rights" educational opener vs "still has options" emotional opener

OpenLetter likely supports % split per template — Mike sets 50/50, results flow back to DataSift activity, Friday weekly review compares response rates.

---

## What to load first (priority order for Mike)

If mail program is launching fresh, build cadences in this order:

1. **Probate (4 templates)** — highest-converting niche per industry data, lowest urgency = forgiving margin if anything's off
2. **Redemption RW-1 + RW-1-RUSH (1 template, 2 triggers)** — Phase 1 unblock; the niche we've designed for
3. **Sheriff Sale SS-1, SS-2, SS-3 (3 templates)** — Phase 1 also; fires when records hit FTM_SS preset
4. **Lis Pendens LP-1 through LP-4 (4 templates)** — Phase 2 expansion; lower urgency = lower priority for the launch sprint

Each template takes ~10 min to load + map variables + set cadence trigger. Total: ~2 hours of Mike's time to get all 12 templates live.

---

## See also

- [SOP-DIRECT-MAIL-PLAN.md](SOP-DIRECT-MAIL-PLAN.md) — economics, ramp plan, mailability filters, ROI math
- [SOP-REDEMPTION-WINDOW.md](SOP-REDEMPTION-WINDOW.md) — full redemption operational guide; the templates here are the mail layer of that niche
- [SOP-CALL-SCRIPTS.md](SOP-CALL-SCRIPTS.md) — SMS + phone scripts that run in parallel to these mail cadences
- [SOP-TAG-FLOW.md](SOP-TAG-FLOW.md) — which tags drive which presets which trigger which cadences
