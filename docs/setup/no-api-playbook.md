# The no-API playbook

Every paid step in this library has a route that needs no API key. They are
slower and they need you in the loop, but they produce the same answer, and
several of them are what the paid path was built from in the first place.

Three techniques carry all of it:

1. **Browser automation.** Playwright drives a site the way you would. This is
   how the DataSift skills work already: no API access, just your own login.
2. **Claude in Chrome.** The extension reads and acts on pages you are signed
   in to. Best where a site actively blocks automation.
3. **By hand, with Claude doing the reading.** You paste or screenshot, Claude
   structures it. Slow per record, free, and often the most accurate.

Pick by volume. Under about 20 records, do it by hand. Over a few hundred, the
paid API is cheaper than your time.

---

## Comping without OpenWeb Ninja
<a id="comping-without-an-api"></a>

**Use the `real-estate-comping` skill.** It is not a degraded fallback, it is
the full method with manual comp collection, and it ships with the same
two-bucket ARV logic and adjustment tables as the paid path.

What changes: you gather the sold comps yourself from Zillow, Redfin,
Realtor.com and Homes.com instead of the API returning them.

What does not change: the boundary discipline, the condition bucketing, the
bedroom-band rule, and the dual-track ARV all still apply, and those are what
actually decide the number.

Practical notes:

- Pull from at least two sites. They disagree, and the disagreement is
  information.
- In non-disclosure states the listing sites will not show sold prices. The
  skill routes you to the county assessor instead, which is free everywhere.
- Screenshot the comp table and hand it to Claude rather than retyping it.
- The 41-row cap that forces price-band partitioning on the API does not exist
  when you browse, so a wide search is genuinely easier by hand.

**When to buy the key.** If you are comping more than about five properties a
month. 100 lookups a month are free, and a single property is several lookups.

---

## Market research without a DataSift login
<a id="market-research"></a>

`sift-market-research` drives Market Finder in a browser, so it already needs
no API. If you have no DataSift account at all:

- Census QuickFacts and the ACS give you population, income, ownership rate and
  vacancy for any county or ZIP, free.
- FRED carries days-on-market, median sale price and inventory by metro.
- Your county assessor's sales file gives real transaction volume, which is the
  number that actually matters. `first-market-county-data` tells you where it
  lives for your county.

Score the ZIPs yourself with the same six weights the skill uses: distress 30,
value 20, equity 15, tax delinquency 15, competition 10, days on market 10.
The weights are the method. The data source is interchangeable.

---

## Presets by hand
<a id="presets-by-hand"></a>

`sequential-presets` clicks through the DataSift filter UI for you. Without the
automation, ask Claude for the preset definition and build it yourself:

> Give me the filter definition for the "00. Needs Skipped" niche sequential
> preset: every filter block, the exact values, and the order.

The skill's references carry all 21 preset definitions as data. Reading them
out and building 21 presets by hand is about an hour, once, and then they exist
forever. The automation is a convenience, not a dependency.

Same for the Sold exclusion: it is one filter block added to each preset.

---

## Sequences by hand
<a id="sequences-by-hand"></a>

`sift-sequences` builds sequences through the UI with drag and drop, which is
the fiddliest automation in the library and the most likely to need a retry.
Doing it by hand is often faster.

Ask for the definition, then build it:

> Give me the full definition for the HOT A01 sequence: trigger, conditions,
> every action in order, and the delay on each.

All 26 templates are in the skill's references. The value is the template
design, not the clicking.

---

## KPIs by hand
<a id="kpis-by-hand"></a>

`kpi-engine` mints a token from your own DataSift login, so it is not really an
API-access question. If you would rather not automate at all:

Export the activity log from DataSift for the period, then hand Claude the CSV:

> Here is my activity export. Give me dials, contacts, correct numbers, leads
> including new_lead statuses, and talk time, per caller and account-wide, then
> grade it against these targets: 2 to 3 leads per day, 15 to 20 leads per
> contract.

The one thing to watch is the lead count. Most manual reports miss the
`new_lead` statuses and undercount leads, which makes every downstream rate
wrong. Say the words "including new_lead statuses" and you avoid it.

---

## Phone scoring without Trestle
<a id="phone-scoring-without-trestle"></a>

Trestle scores a number for line type, activity and litigator risk at about
$0.015. Without it, you can recover most of the dial-order value free:

- **Line type** from a free carrier lookup. Mobile beats landline for SMS and
  for pickup.
- **Disconnected numbers** surface on the first dial. One pass through a list
  removes them permanently, so the cost is one wasted call each.
- **Order by source instead.** A number that appeared in two independent skip
  trace sources outranks a single-source number, and that is free to compute:
  it is just counting.
- **Recency beats everything.** The most recently reported number for a person
  outperforms an older one regardless of score.

Keep the same five tiers so the rest of the system still works: 81 to 100 dial
first, 61 to 80 second, 41 to 60 third, 21 to 40 fourth, 20 and under drop.
Assign them from what you know rather than from a score.

**The one thing you cannot replicate.** Litigator and TCPA risk flagging. If
you are dialing at volume, that check is worth the money on its own, and it is
the reason to buy the key even if you skip everything else.

---

## Heir research by hand
<a id="heir-research-by-hand"></a>

`deep-prospecting-v5` costs about $0.24 a record and it is the best value in
the library. But the free version is genuinely good, and it is where the paid
method came from.

The chain, per decedent:

1. **Confirm the death.** Obituary sites, funeral home notices, FindAGrave.
   Free and authoritative.
2. **Check the decedent against the owner of record** before anything else.
   This is the single most important step and it is free. An obituary on a
   record does not mean the owner died: it is frequently the spouse. Getting
   this wrong means asking a recent widow for her dead husband.
3. **Pull relatives from the obituary itself.** Survivor lists name the
   children, the spouse and often the grandchildren, in order, with married
   surnames. This is better data than most paid relative graphs, because a
   human who knew the family wrote it.
4. **Find the probate filing.** County clerk or court portal, usually free and
   searchable by name. It names the personal representative, who is the person
   with authority to sign.
5. **Get phone numbers** from free people-search sites: TruePeopleSearch,
   CyberBackgroundChecks, FastPeopleSearch. Expect to work around bot walls.
6. **Rank the signers** by TN-style intestacy order, or your state's, which the
   skill's references carry.

This takes about 20 to 40 minutes a record versus a few minutes paid. At low
volume that is fine. At any real volume, buy the key.

**Do not skip step 2 just because you are in a hurry.** It is the check the
paid path cannot do for you either.

---

## Spam flag checks by hand
<a id="spam-flag-checks-by-hand"></a>

`caller-reputation-monitor` polls Telnyx for the reputation of every outbound
number. Without it:

- **Call your own numbers** from a phone on each major carrier and look at what
  the screen says. Tedious, completely accurate, and free. Once a week per
  number is enough to catch a flag early.
- **Register with the free carrier lookup portals.** The major US carriers each
  run one, and they will show you a number's current label and accept a
  remediation request.
- **Watch your own answer rate per number.** A number that was answering at 8
  percent and drops to 2 has almost certainly been flagged, and you will see
  that in your dialer stats before any portal tells you.

The mitigations are the same either way and they matter more than the
monitoring: rotate numbers, keep per-number daily volume low, never let one
number carry a whole campaign, and retire a flagged number rather than trying
to rehabilitate it while still dialing from it.

---

## Coaching without a dialer API
<a id="coaching-without-a-dialer-api"></a>

The three coach skills pull recordings from SmrtPhone. If you use a different
dialer, or none:

1. **Export the recordings.** Every dialer has a download, even if it is one at
   a time. You only need a handful: five real conversations tells you more than
   fifty voicemails.
2. **Transcribe.** Any audio model works. If you would rather not use one at
   all, most dialers ship a built-in transcript, and free transcription tools
   are adequate for grading.
3. **Grade against the rubric.** This is the part that matters and it needs no
   integration:

> Grade this call against the cold-calling rubric: opener, motivation probing
> across the four pillars, objection handling, tonality, and close quality.
> Score each, quote the exact moment it went wrong, and give me one thing to
> change on the next call.

The rubric lives in the skill's references and is the actual product. The
pulling and transcribing are plumbing.

**One thing to keep.** Grade only real conversations. Voicemails and wrong
numbers are not calls, and scoring them drags every average down and hides the
signal in the calls that were real.

---

## What has no substitute

Being honest about the limits:

- **Litigator and TCPA risk screening.** If you dial at volume, buy it.
- **Bulk skip trace at scale.** Free people-search sites bot-block hard and do
  not do batches. Under about 50 records a month you can work by hand. Above
  that you cannot.
- **Deed-level buyer data.** Some of this exists only behind a paid parcel API
  or a title account. County records have it, but assembled by hand it is
  days of work per county rather than minutes.

Everything else in this library has a free route that gets you to the same
decision.
