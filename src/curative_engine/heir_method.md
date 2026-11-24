# Curative heir story: the research method

You are researching ONE house where the owner of record has died (or may have died). The goal is the full story
a buyer needs before calling anyone: who owns the house now (the heirs and their shares under the state's law
when there is no will), how sure we are, how to reach each heir, and what could stop a deal. The method was
written from a case where the first answer was wrong: a "daughter" the team was working turned out to be a
stepchild with no share, and the real heirs were children from an earlier marriage the family had lost touch with.

Everything below is internal research. Nobody is contacted.

## Hard rules

1. Research only. Never write to DataSift or any CRM, never text, call or email anyone, never post anywhere, never
   buy anything. Paid checks (SmartSkip, Trestle) are run later by the person running this, from what you write.
2. Write only inside this folder (subfolders are fine). Delete nothing anywhere, including in this folder.
3. Read nothing outside this folder except public web pages and the tools named in "This run" above. Never open a
   `.claude` folder, a `.env` file, or any other file on this computer.
4. Never print or save API keys, passwords or tokens.
5. Never use the em dash or en dash characters in anything you write. Use commas, colons, parentheses or periods.
   Number ranges use a plain hyphen (1-3).
6. No guessing. Every person in the family needs a source. Every heir needs a record that names the decedent (or the
   person the heir inherits through) as the heir's parent. "Unknown" is a valid answer; a wrong heir is not.
7. Never write that we represent the estate. Never suggest stopping or delaying a foreclosure. We are not lawyers:
   the title company or the attorney confirms who inherits.
8. If a tool call is refused or fails twice, change approach instead of retrying the same call.

## Your inputs (`input.json` in this folder)

- `deed`, `deed_count`, `roll_owner`, `roll_people`: who holds title. `roll_mail`, `owner_occupied`: where the
  county sends the bill and whether the owner seemed to live there. `state`, `county`: where the house is.
- `decedents`, `death_day2`, `conf_txt`, `evidence`, `index_checks`, `obituary_index_rows`: what we already know
  about the death. Where the county has an obituary index, its rows list every obituary by name with the age and
  the date; several people often share a name, so match on age.
- `family_tree`: the SmartSkip trace of the owner, when one was run. Its relationship labels are unreliable: most
  relatives read "Relative" or "In-Law", it marks the living as dead and the dead as living, and a "Daughter" can be
  the spouse's daughter (a stepchild). Treat every name as a lead, never as proof. Phones in it are real numbers
  for those people.
- `crm_says`: what the CRM recorded, if anything (for example decision makers). Unverified. It may be wrong.
- Money and pressure: `avm` (value), `appraised`, `equity`, `mortgage`, `tax_years`, `tax_amount`, `city_years`,
  `lien_amount`, `fc_ahead`, `sale_day` (a foreclosure sale), `listed`, `mls`, `sold_after`.
- `probate_index`: where the county publishes an index of probate cases, the cases opened after each dead owner's
  death under the same name: docket, type, opened, status, closed, will book, and what dates the index covers.
  A name match is a lead, not proof: confirm it is the same person (the opened date against the death, a middle
  name, the Title column). "No case found" covers only the dates the index covers.

**When `probate_index` shows a case for the last owner to die,** confirm it is the same person, then:
- TESTATE (any variant) or MUNIMENT OF TITLE: a will was probated. The will, not the no-will shares, decides who
  owns the house. Put the docket, type, dates and will book in `will_or_probate`, name the executor and the people
  the will leaves the house to if you can find them, write a short story.md, and stop: it is not a curative deal.
  Use no more than about 8 searches on such a house.
- INTESTATE or PROBATE and OPEN: an estate is open with an administrator. Record it in `will_or_probate` (docket,
  type, opened) and research the heirs as usual: it is a probate deal, and the heirs are still who will sign.
- INTESTATE and CLOSED: the estate was administered and closed. Start `will_or_probate` with "closed intestate
  estate:" and then the docket and dates, and research the heirs as usual (the house can still sit in the dead
  owner's name with the heirs holding shares).
- DISMISSED, or a case that turns out to be a different person: start `will_or_probate` with "none" and then say
  what the index showed and why it does not count.

## Steps

1. **Deaths.** For each title holder: alive or dead, date and place of death, from an obituary, a Find a Grave
   memorial, a Social Security death or NUMIDENT entry, or an obituary index row whose age matches. A SmartSkip
   deceased flag counts as a death, but find the date. Two traps: the obituary on file may be the co-owner's (the
   spouse-obituary trap), and a same-name stranger's obituary (check age, spouse, town, the house).
2. **Whose estate holds the house.** Two names on the deed and they were married: at the first death the survivor
   usually took the whole house, so it sits in the estate of whoever died LAST. Two names and not married
   (siblings, parent and child): each holds a share; follow each share. One name: that person's estate. Record any
   sign of a will, an executor or a probate case; a will changes everything, so flag it.
3. **That person's family.** Find every marriage: the obituary ("preceded in death by his first wife", "married
   for 40 years"), marriage and divorce indexes, the 1950 census. Find every child and tie each one with a record
   that names THIS person as the parent: the decedent's own obituary saying "his son" or "her daughter" (watch
   "their" and "her" when the spouse had children of her own), the child's own obituary or birth record ("born to"),
   a sibling's obituary listing the siblings, a Social Security entry naming the parents, a birth index. The
   spouse's children are stepchildren and inherit nothing unless adopted. Children named in a spouse's obituary are
   that spouse's children: check each one against the person whose estate holds the house.
4. **Children who died first.** Their own children take that child's share. Prove those grandchildren are that
   child's own: an obituary's "his two daughters" can be his wife's daughters.
   **Earlier marriages are where heirs hide.** Treat an earlier marriage as likely, and look for it, when the
   person married their last spouse after about age 30, served in the military, or lived in other states. Check
   every state in their address history for marriage, divorce and birth index entries (a divorce entry can show the
   number of children), their parents' and siblings' obituaries (they list grandchildren and nieces), their own
   Find a Grave family links, and children with the same surname born before the last marriage. A child from an
   earlier marriage inherits the same share as any other child, and the family the house passed through may have
   lost touch with them. If you cannot rule an earlier family in or out, say so in open_questions and set the
   overall confidence no higher than "medium".
   **Before you call a child unproven,** try all of these: the obituary on legacy.com and on the funeral home's own
   site (search the funeral home name with the decedent's name), Find a Grave for the decedent AND the spouse
   (family links), the probable child's own people-search record (their relatives list should show the decedent
   as a parent at the right age gap), the spouse's obituary if the spouse died, sortedbyname birth and marriage
   entries, and newspaper snippets in search results. When no record is online, two independent data sources that
   both tie the person to the decedent (for example the SmartSkip tree and a people-search record), with ages and
   towns that fit, are a MEDIUM tie: list the person as an heir with confidence "medium" and name the record that
   would confirm it. Use your search budget before you leave an heir unproven.
5. **No spouse and no children.** Then parents, then brothers and sisters (half-siblings count), and the children
   of a brother or sister who died first. A parent's obituary usually lists the siblings.
6. **Not heirs.** List each person who looks like family but does not inherit, with the reason and the source
   (stepchild, in-law, former spouse, died first with no children).
7. **Shares (no will).** Apply the intestacy law of the state where the house sits (`state` in your input).
   - Tennessee: a surviving spouse with descendants takes the greater of one third or a child's share, the rest to
     the descendants; a spouse with no descendants takes everything. Descendants share equally, with a child who
     died first replaced by that child's own children (by representation). No spouse or descendants: the parents;
     then the siblings by representation.
   - Any other state: find that state's intestate succession statute, apply it, and cite it (the statute number
     and a link) in story.md. Say plainly that a lawyer has not reviewed it.
   Write each heir's fraction.
8. **Find each living heir.** City and state, the street where found, age, and any phone numbers already in your
   input or returned by the people-search tool (if this run has one). Its relatives list is a lead only: it mixes
   in unrelated same-surname families, so never accept a person without a named parent or sibling tying them.
9. **The situation.** Who lives in the house now, if anyone. Whether it is listed or sold since. The foreclosure
   sale date, county and city tax years behind, liens. Anything that blocks a deal (a non-selling heir living there,
   a will, an open probate, a sale already done).
10. **Write the outputs** (below).

## Sources that work

- **Obituaries:** search `"<Full Name>" obituary <town> <year>`; legacy.com, funeral home sites, the local paper.
- **Find a Grave:** memorials link parents, spouses and children, with dates. The memorial's creator is usually a
  volunteer, not family.
- **sortedbyname.com:** Social Security NUMIDENT entries (name, birth, death, sometimes the parents) and county
  marriage licenses, as plain pages you can read.
- **The county's obituary index,** when the input carries its rows (search it again by surname for the family).
- **A sibling's or parent's obituary:** it names all the siblings with spouses and towns, and its "born to" line
  proves the parents. Searching a parent's full first and middle name lands on a child's obituary.
- **State birth, marriage and divorce indexes:** for several states, public search pages show names, counties
  and parents' given names without a subscription. Do not log in anywhere.
- **The 1950 census** (free) for the owner's parents and siblings.

## Budget

Your brief gives your limits for web searches, page reads and people-search lookups. Stop when every heir is tied
by a record, or when you reach the limits; then write down what is still open. Do not keep searching to make a weak
answer look strong.

## Outputs (in this folder)

### heirs.json

```json
{
  "house": "<address>",
  "slug": "<slug>",
  "researched": "<YYYY-MM-DD>",
  "title_holders": [{"name": "", "alive": "no|yes|unknown", "born": "", "died": "", "place": "", "sources": [""]}],
  "whose_estate": "<the person whose estate holds the house now>",
  "title_path": "<plain words: how the house came to sit in that estate>",
  "will_or_probate": "none found | <what was found>",
  "heirs": [{"name": "", "relationship": "son of <decedent>", "share": "1/5", "share_num": 0.2,
             "living": "yes|no|unknown", "age": "", "city": "", "state": "", "street": "",
             "tie": "<the record that names the parent, quoted briefly>", "tie_source": "<url>",
             "phones_found": ["10 digits"], "confidence": "high|medium|low"}],
  "not_heirs": [{"name": "", "why": "", "source": ""}],
  "heir_count": 0,
  "equity_per_heir": 0,
  "confidence": "high|medium|low",
  "occupancy": "<who lives there, if known>",
  "blockers": [""],
  "open_questions": [""],
  "trace_requests": [{"name": "", "street": "", "city": "", "state": "", "zip": "", "age": "", "why": ""}],
  "sources": [""],
  "counts": {"web_searches": 0, "page_reads": 0, "people_search": 0, "minutes": 0}
}
```

`trace_requests`: each living heir who still needs phone numbers or a current address (a skip trace and a phone
check run later on these). `equity_per_heir`: the input `equity` times the smallest heir share.

### story.md

One page plus tables, plain words, in this order:

1. `# <address>: heir story (<date>)`
2. **Bottom line.** Two to four sentences: who owns it now and in what shares, how sure we are, the clock.
3. **Who died, and in what order.** Table: person, born, died, source.
4. **Whose estate holds the house, and why.** The deed form, the order of deaths, the rule.
5. **The family.** A plain text tree in a code block, marking heirs and not-heirs.
6. **Not heirs, and why.** Bullets.
7. **How each heir was identified.** Numbered, each with the record that ties them.
8. **Who must sign.** Table: heir, share, relationship, where they live, age, numbers (write "pending check";
   numbers are verified later).
9. **The situation.** Occupancy, sale date, taxes, liens, value, equity and equity per heir.
10. **Blockers and open questions.**
11. **Sources.** URLs.

### Your final reply

Five lines or fewer: the bottom line, the heir count and confidence, the biggest open question, and one line with
your counts (web searches, page reads, people-search lookups, minutes).
