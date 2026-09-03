# Copy rules, and the audit that enforces them

## Voice

Write the way the person signing it writes. Concretely:

- No throat-clearing opener. Not "Ty here" — sign off at the bottom instead.
- Contractions. Short sentences. One question, at most.
- Offer, do not demand: "if you're interested I can send the walkthrough
  video", not "I want to send you the video".
- Acknowledge why you are texting them: "I saw you were a potential buyer"
  beats "I buy and sell a few houses".
- No em dashes, no semicolons, no links, no emoji, no ALL CAPS. Those read as
  bulk mail.

## Say only what you know

Name the location from **each buyer's own purchase history**, never the
subject's. Claiming a buyer works a sub-market you have not verified is a lie
they can catch, and it is the kind that ends the conversation.

Never imply a price relationship that is not true. "Another one at this price"
on a $104,000 deal, sent to people who saw a $75,000 one, is a factual claim
that reads as careless or as bait. Reference the relationship instead: "since
you looked at the last one".

## Addressing people is where it goes wrong

Four failures, each from one real row:

| Input | Wrong output | Rule |
|---|---|---|
| `E J E Bourgeois` | "Hi E," | A bare initial is not a first name. |
| `J A Murphy Group Llc` | "Hi Murphy," | A person-name rule applied to a company. Check entity first. |
| `Haddad Amer Michael` | "Hi Haddad," | County records write LAST FIRST. |
| `William David Faulkner Sr` | "Hi William David Faulkner Sr team," | A person is not a "team". |

An entity has an oracle for the last-first problem: the company name. A bare
person name has none, so a person name of 3+ tokens is **ambiguous and gets no
addressee at all**. That costs a correct "William" on one row to avoid a wrong
"Haddad" on another, which is the right trade in a cold text: no name reads
fine, the wrong name reads like a list.

For entities, keep the brand intact. Strip only the legal suffix; PROPERTIES,
HOLDINGS, GROUP and PARTNERS are part of the name. If a message runs long, fit
it by choosing a shorter VARIANT, never by trimming the company's name — that
produced "Affordable Houses and Real team".

When you cannot name the signer, say so. "Hi Smithbilt team (sorry, couldn't
find the signing member)" is more honest, and reads better, than pretending.

## The audit

Run it over EVERY rendered message before staging, and refuse to stage on
failure. Variants are hash-selected, so a sample does not represent the batch.

Assert each message:

- signs off with the chosen sender's name
- contains the approved price and **no other money token**
- carries no link, no stray zip, no em or en dash
- asks at most one question
- is under the length ceiling
- names a place that buyer has actually bought in
- carries whatever the campaign promised: the offer, the address, the cue

Check the sign-off against the **chosen** sender, not a hardcoded name. The
sender itself must be forced rather than defaulted: reading it from a record's
assignee made one message in 156 sign as a different person, on a deal they
knew nothing about.

## Rotation

Three to four variants per touch, selected by a hash of the record so the same
buyer gets a stable message. Keep the variants genuinely different in structure,
not just in synonyms, or the rotation is cosmetic.
