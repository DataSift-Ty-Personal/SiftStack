# Obituary and web research layer

This is the layer that replaced the Enformion person search for death data, and it is
**mandatory** on any record where a death is suspected. SmartSkip supplies the people;
this supplies the truth about who died and how they were related.

## Why this is safe now (and was not in v3)

v3 asked an LLM to read obituary prose and *produce the heir set*. It hallucinated whole
families. v5 never asks that. SmartSkip hands over a grounded relative list from data,
and this layer only has to do two much narrower jobs:

1. Confirm or correct the relationship labels on people we already have.
2. Supply the date of death and confirm whose death it was.

Inventing a person is no longer a possible failure mode, because the person list does
not come from here.

## The checks, in order

### 1. Whose obituary is it?
**Run this before anything else.** Match the decedent's name against the owner of record.

A live record carried an Obituary list membership and a recent obituary date. The
obituary was the owner's **husband's**, not hers. She was alive and owned the property.
Treating it as an heir case would have had a caller ask a recent widow for her dead
husband.

If the decedent is not the owner, the record is a **living owner with a recent household
death**. That is usually a *better* lead than an heir case, and it needs a completely
different opening. Correct the record: it should not sit in a deceased-owner queue.

### 2. Pull the verbatim survivors paragraph
Funeral-home pages are the best source. Quote it exactly into the research pack. Do not
paraphrase; the exact wording carries relationships ("and husband [name]") that a summary
loses.

### 3. Reconcile both directions
- Relatives SmartSkip found that the obituary does not name: keep, but label unconfirmed.
- Survivors the obituary names that SmartSkip missed: **married-out daughters are the
  classic miss** because the surname changed. These are often required signers.

### 4. Overwrite coarse relationship labels
SmartSkip's "Possible Type" ran 63% generic on a live batch and mislabeled a 62-year
husband as a plain "Relative." The obituary's language is authoritative; use it.

### 5. Date of death, then the sanity check
Record the DOD. Reject a match whose DOD is more than **3 years** before the notice's
**publication** date. A 2004 date surfaced on a live record during validation; that is
the exact false-match this check exists to catch.

### 6. Conflicts
If two sources disagree on a date of death, **surface the conflict, never silently pick
one.** A second household death in the same period is common and is the usual explanation.

## Fetching pages that block

County, records and genealogy portals (assessor and deed datalets, FindAGrave, Legacy,
funeral homes, court info pages) often sit behind Cloudflare or a JS wall. Use the
Scrapfly fetcher for those; it is the sweet spot.

It does **not** reliably work on hardened people-search aggregators
(TruePeopleSearch, FastPeopleSearch), which frequently IP-ban it. And records a county
simply does not publish online cannot be fetched at all; those are phone or in-person.

## Useful searches

- `"<decedent name>" obituary <city> <state> <year>`
- `"<decedent name>" "<owner name>"` to catch the spouse-obituary case directly
- `"<owner surname>" obituary <city>` when the first name is uncertain
- Add `site:legacy.com`, or the local funeral home domain, to cut noise

See `google-dorking.md` for the fuller pattern set.
