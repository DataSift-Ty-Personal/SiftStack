---
name: dispo-deal-blast
description: >
  Text one named wholesale deal to the cash buyers whose own deed history says they buy at that price, and hand every reply to a human. Builds a buyer registry from investor transactions, gates it on recency, matches an asymmetric price band, renders copy that cannot leak the contract price or the address, stages the batch as HELD behind a deliberate release, and turns the responses into a permanent buyer phonebook. Trigger for: blast a deal to my buyers, dispo this property, text my cash buyer list, who should I send this deal to, build a cash buyer list, or set up dispo SMS.
---

# Dispo Deal Blast

Send one deal to the buyers who actually buy at that price, then turn what comes
back into an asset you keep.

Most dispo lists are a spreadsheet of everyone who ever bought a house. This
builds the opposite: a small list of people whose recorded purchases say they
buy this kind of property at this kind of price, reached on a number they have
heard from before.

Use when someone says "blast this deal", "who do I send this to", "text my
buyers", or "set up dispo texting".

## Requirements

- Python 3.10+ (the bundled script is stdlib only)
- A CRM or data source with deed-level investor transactions
- An SMS provider with numbers you own, and a compliance posture you understand

Everything below is the method. None of it needs a key to read, and the bundled
calculator runs on plain Python.

## The chain

```
registry  ->  recency gate  ->  price band  ->  copy  ->  staged blast  ->  phonebook
```

1. **Registry.** Sweep investor transactions in your county. Hydrate each
   property for the deed owner, because search results usually carry no name.
   Dedupe by mailing address. Resolve LLC principals.
2. **Recency gate.** Re-run the sweep bounded to the last 12 months. Recency
   decides WHO is on the list; full history decides WHAT they buy.
3. **Price band.** Each buyer's real purchase range, p10 to p90, from priced
   sales only.
4. **Copy.** One message per buyer, audited before anything is staged.
5. **Staged blast.** Everything lands as HELD. Release is a separate human act.
6. **Phonebook.** Classify every reply, suppress the outs, keep the buyers.

## The rules that matter

Each of these cost a real failure to learn. They are the actual content of this
skill; the code around them is easy.

### The semantic trap that ruins the list

Transaction labels describe the LAST SALE, not the current owner.

- `pending`, `wholesale`, `wholetail`, `rental` mean the investor still holds,
  so the **current owner IS the buyer**. These are your targets.
- `flip` means the exit already happened, so the current owner is a **retail
  homebuyer**. The investor you want is the last-sale SELLER.

Texting the `flip` bucket reaches ordinary families who just bought a house.

### Probe every filter by count delta

An unknown filter key is **silently ignored**. A deliberately bogus key returned
a byte-identical count to no filter at all. Acceptance proves nothing: change
one filter, compare the count, and only then trust it.

The same applies to search scoping. A county field that wants `"Knox"` returns
zero for `"Knox County, TN"`, and zero is indistinguishable from an empty
segment.

### The price band is asymmetric

Above a buyer's band is **affordability**: someone whose ceiling is $120k cannot
close $600k. Below it is only **interest**: someone whose cheapest purchase was
$140k can obviously afford $75k, they may just not want one that small.

A symmetric tolerance kept 68 of 199 buyers at a $75,000 ask and dropped people
for being too big, which is the wrong reason to skip anyone. Divide the floor by
a multiple; keep the tolerance on the ceiling.

### "No institutional buyers" cannot be a keyword rule

Scanning for HOMES / BUILDERS / CONSTRUCTION flagged 24 names on a real cohort.
**23 were small local operators** with 1 to 11 doors. A self-performing local
builder is the single best buyer for a heavy rehab, so the keyword sweep deletes
the target audience to catch one name. The same failure hits `BANK` inside
`WILLBANKS`.

Use a named list of firms you can verify are production builders, iBuyers or SFR
funds, and log every drop with the name it matched.

### Address discipline, both directions

Default to withholding the address: name the road, and let the OFFER do the
redacting ("I can send the walkthrough, photos and full address if you're
interested"). Saying it defensively ("not posting the address here") draws
attention to the withholding.

Disclosing the address is an explicit **per-deal opt-in**, never a code change,
so the redaction default keeps protecting every other campaign.

### The price whitelist beats a blocklist

Never state a figure other than the approved asking price. Implement it as a
whitelist: extract every money-looking token and require each to equal the
approved price. A blocklist only catches the number you remembered to ban; a
whitelist catches the contract price **without the audit ever being told what it
is**, which means the contract price never has to enter the process.

### Copy is audited on every message, not a sample

Variants are chosen by a hash of the record, so the four messages a sample
prints are not the four that ship. Three variants once shipped unsigned and only
two surfaced in a sample. Assert on all of them, and refuse to stage on failure
rather than printing a warning above a staged batch.

### Sticky senders

A buyer who replied to one number and then hears from another reads as a spam
farm, and a callback rings a line with no history of them. Pin each buyer to the
number that actually texted them last, and verify it by joining the new batch
against the old one before releasing.

### Staging twice is the worst thing a cold number can do

Staging is a WRITE. A dry-run flag that gates sends but not staging will report
itself as a dry run while writing the whole batch. Add a duplicate guard that
skips any phone already holding a pending message, and return the count so a
caller cannot report more than it staged.

## Human gates

Keep these with a person, permanently:

- **Release.** Staging sends nothing; release is the irreversible step.
- **The address, photos and lockbox code.** The agent offers them; a human sends
  them. A lockbox code is physical access to a house and never belongs in an
  agent's facts.
- **Any reply that names a price.** Negotiation is not an agent's job.

## Files

- `references/buyer-registry.md` — sourcing, dedupe and principal resolution
- `references/sms-guards.md` — every gate and the failure it prevents
- `references/copy-rules.md` — the voice rules and the audit
- `scripts/cohort.py` — stdlib cohort and band calculator
