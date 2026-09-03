# Every gate, and the failure it prevents

None of these are theoretical. Each one is here because something went wrong.

| Gate | The failure it prevents |
|---|---|
| Suppression list | Texting someone who said stop. Decide opt-outs by rule, never by a model, and cover natural language ("take me off your list") not just the STOP keyword. |
| Human takeover pause | The agent talking over a person mid-negotiation. An outbound message we did not author means a human stepped in. |
| New-deal reopen | The inverse failure: a paused thread from the LAST deal excluded every engaged buyer from the next one. A new deal may reopen a paused thread, but never an opt-out. |
| Duplicate guard | Staging twice. One batch staged twice produced 312 rows for 156 buyers; released, every one would have been texted twice. |
| Dry-run gate on staging | A "dry run" that still writes. Staging is a write; gate it explicitly. |
| Sticky sender | A buyer hearing from a different number than last time, which reads as a spam farm and rings a line with no history of them. |
| Pinned pool | A program falling back to another program's numbers. Spends the other program's carrier budget invisibly, puts two programs on one number, and misroutes callbacks. A pinned pool that is missing must return NOTHING. |
| Per-pool daily cap | One program's volume decision silently changing another's carrier risk. |
| Per-pool send gap | The same, for pacing. |
| Two send windows | Both your business hours AND the recipient's local hours. A single fixed timezone cannot express both: 9am Eastern is 6am on the west coast. |
| Output validator | Copy that names a price it should not, carries a link or a zip, asks two questions, or identifies itself as automated. |
| Copy audit | A bad variant shipping because the sample happened not to show it. |
| Transient-error retry | Reading a 429 or a 502 as missing data. Fifteen real buyers were held as "could not read dial tier", which is indistinguishable from an untagged record. |

## The pattern behind most of them

Almost every bug on this system was **a silent fallback standing in for a real
answer**:

- an unknown filter key ignored rather than rejected
- a missing pool falling back to every number in the account
- a missing registry file returning `{}` so the price filter quietly stopped existing
- a rate-limit error caught and returned as "no data"
- a probe whose search pattern appeared in its own command line, so it always
  matched itself and could never fail

When something cannot produce a real answer, fail loudly. A check that cannot
fail is not a check.

## Verify by reading the database, not the render path

Before releasing, read the staged rows out of storage and assert on those. The
render path is what you just changed; it is the least trustworthy thing to ask.

Assert: the expected count, distinct phones, every message carrying the approved
price and nothing else, no link, no stray zip, the sign-off, the length ceiling,
and the sticky-sender join against the previous batch.

## Watch the first sends

No test suite can know that a number is not registered with the carrier. On the
first live run, exactly one number was registered and four returned "register
this Caller ID" — a failure only a real send could reveal. Send one canary per
number, read the result, and halt on failure rather than discovering it at
message 150.
