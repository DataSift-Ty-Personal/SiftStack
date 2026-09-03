# Building the buyer registry

The goal is a list of PEOPLE with reachable phones, not a list of properties.

## Sweep

Filter investor transactions by type. Sweep the HOLD types collapsed by owner,
which is what makes it cheap: one real cohort went from 1,633 properties to
1,140 rows that way. Sweep the EXIT types uncollapsed, because you need each
sale in order to read its seller.

Search rows usually carry **no owner name**, so hydration is unavoidable. They
do carry the CRM record id, which is the join key you want later.

Sale history is normally ordered newest first, and that ordering is
load-bearing: the flip branch reads `sale_history[0].seller_name`. Verify the
ordering on real data before trusting it.

## Recency is the gate, history is the profile

An unbounded sweep has no date filter at all. Measured on a real county, the
purchases ran 2019 to 2026, only 41% of buyers had bought within a year, and
the median last purchase was 456 days old. Half the list had stopped buying.

Run the sweep twice. The bounded run decides who is on the list; the unbounded
run tells you what they buy. Price bands prefer the last 24 months and widen to
full history only when that leaves under 3 priced sales, and the row records
which basis it used rather than hiding the widen.

## Dedupe by mailing address, with a suite guard

Clustering on the mailing address catches what name matching misses:
`DEVELOPERS TEAM 1 LLC` vs `DEVELOPERS TEAM I LLC` (digit versus letter), and
`GDP PROPERTIES LLC` vs `GDP PROPERTIES LLC PRO SOURCE HOME BUYERS`.

Unguarded it over-merges. One suite address hosted two unrelated companies
sharing an agent. So an address carrying `STE|SUITE|UNIT|APT|BLDG|FL|PMB|RM`
requires a fuzzy name match as well, and every merge AND every refusal gets
written to a report with its reason.

## Resolving the human behind an LLC

Cheapest first, because the free step often wins:

1. **Reverse the mailing address.** When an LLC's mailing address is a
   residence, look up who lives there. On a real registry this resolved 1,173
   principals for $0, about a 40% hit rate on entities.
2. **Corporate filings**, for what that missed.

Three cautions, all measured:

- **The registered agent is usually the company's lawyer.** 135 of 193 officers
  came back titled AGENT. Trust an ownership title outright; trust an agent
  title only when a name token also appears in the company name.
- **Corporate search cannot be constrained geographically.** Searching with no
  anchor, with a city, and with a state returned byte-identical results. It
  fuzzy-matches nationally, so a local LLC resolves to officers of a same-named
  company in another state. Of 12 checked entities, only 2 had an in-state
  officer. Filter on the officer's own address.
- **A filing-sourced officer cannot be skip traced at the company's address.**
  Reverse-address principals hit 95%; filing-sourced ones hit 4%, because skip
  trace matches name plus address and the officer has no established link to
  that address. Trace them at their own address instead: that took the same
  batch from 4% to 77%.

## Names are dirty in specific, fixable ways

- **County records write LAST FIRST.** "Haddad Amer Michael" is Amer Michael
  Haddad. When the person came from an entity there is a free oracle: whichever
  token also appears in the company name is the surname.
- **A legal-status token is not a surname.** "Morales Family Trust" resolving to
  last name "Tr" is wrong.
- **A bare initial is not a first name**, and a single surname is not a person.
- **Placeholders sneak through.** One source returned the literal string
  `UNKNOWN` as an owner name and it passed every entity check.

## Check what you already own before buying

A comment claiming the CRM could not skip trace entity-owned records went
unmeasured for months. It was wrong: of 128 unreachable buyers, **107 already
had phones** on records the account had already paid to trace. Harvest those
first. An assertion in a comment is not a measurement.
