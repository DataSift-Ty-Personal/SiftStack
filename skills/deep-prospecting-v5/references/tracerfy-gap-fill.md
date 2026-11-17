# Tracerfy gap-fill

Tracerfy is the **bolt-on**, not a primary source. SmartSkip already returns a phone
for about **93% of the relatives it names**. Tracerfy exists to close the remaining 7%.

## When to call it

Only for a person SmartSkip **named but returned zero phones for**. Do not re-trace
people who already have numbers; you would be paying to rediscover what you have.

On a live 100-record batch:
- 682 relatives returned
- 637 already carried at least one phone
- **45 had none** -> the Tracerfy target list
- all 45 carried a mailing address to anchor on
- cost to close the gap: **$0.90**

On a single record the gap is very often zero. Check before spending.

## Input shape

Tracerfy anchors on name plus address, the same shape the SiftStack batch path uses
(`NoticeData` with `owner_name`, `address`, `city`, `state`, `zip`). SmartSkip gives
every relative a mailing street, city, state and zip in the parse output, so the
anchor is already in hand.

```python
from tracerfy_skip_tracer import batch_skip_trace
notices = [NoticeData(owner_name=r["name"], address=r["mailing_street"],
                      city=r["mailing_city"], state=r["mailing_state"],
                      zip=r["mailing_zip"])
           for r in relatives if not r["phones"] and r["mailing_street"]]
stats = batch_skip_trace(notices, max_signing_traces=1, lookup_heir_addresses=False)
```

## Cost and gotchas

- **$0.02 per record** on the batch path.
- The **`deceased` boolean lives on Tracerfy's instant-lookup path, NOT the $0.02
  batch path**, and it carries no date of death either way. Do not plan to get death
  data here. Death data comes from the obituary and web pass.
- Numbers found by Tracerfy are tagged with their own source tag, so cross-source
  confirmation stays auditable. A number found by two independent sources is
  materially stronger than one found by either alone.
- Feed everything Tracerfy returns into the same global TrestleIQ dedupe before
  scoring.
