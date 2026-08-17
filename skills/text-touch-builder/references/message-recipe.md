# Text Touch Message Recipe

The rules and variant pools behind the four-touch sequence. Sourced from a top-performing SMS wholesaler's playbook (the identity-check drip system) blended with cold-email deliverability practice (copy rotation).

## Non-negotiable rules

1. **One job per touch.** Touch 1 verifies identity. Touch 2 resends. Touch 3 asks softly. Touch 4 says goodbye. Never combine jobs.
2. **Short.** Target under 160 characters (one SMS segment). Hard cap 320. Shorter reads more human.
3. **Personal merge fields:** owner first name, street address (street line only, "714 Martha Ln", never the full address with zip), city, and the sender's real first name as sign-off.
4. **Positive and warm.** Blessings, "hope your week is great", "wishing you the best". Never urgency, never pressure, never all caps.
5. **Never name the list.** No foreclosure, probate, tax, auction, distress, "I know you're going through". The seller should feel found, not targeted.
6. **No links, no images, no prices** in these four touches.
7. **Always a question.** Every touch ends with something easy to answer, usually yes/no identity confirmation.
8. **Vary the copy.** Neighboring records must not receive identical sequences. Rotate variants; write more variants in your own voice over time.
9. **The goal is a phone call,** not a text conversation. Once they reply, qualify with: right person? considered selling? price in mind? Then call.

## Sound human, or do not send it

A message that reads as machine-written is worse than no message. It kills
trust on sight, and on SMS it is the fastest way to get reported. Every
generated message and every variant in the pools is checked against this list
by `scripts/build_text_touches.py`; anything that trips it is refused, not
warned about.

**Never, under any circumstances:**

- **The em dash or en dash.** This is the single clearest tell that text was
  machine-written. Nobody produces one from a phone keyboard. Use a comma, a
  period, a colon, parentheses, or the word "and". Number ranges use a plain
  hyphen.
- **Semicolons.** Nobody uses a semicolon in a text message.
- **Links** of any kind. They get the number filtered.
- **Emoji**, stacked exclamation marks, or ALL CAPS words.

**Phrases that instantly read as a machine or a script:**

"I hope this message finds you well" · "I wanted to reach out" · "circle back" ·
"touch base" · "at your earliest convenience" · "please do not hesitate" ·
"feel free to" · "no obligation" · openers like "Absolutely" or "Certainly" ·
essay connectives like "Additionally", "Furthermore", "Moreover"

**Vocabulary nobody uses in a text:**

delve · navigate · landscape · streamline · robust · leverage · utilize ·
seamless · elevate · unlock · empower · tailored · curated · comprehensive ·
myriad · holistic · synergy

**What human actually looks like here.** Contractions. A sentence that trails
off naturally. An apology that is not perfectly balanced. "I'm not even sure I
have the right number." "Sorry to bother you!" "I'll stop bugging you after
this!" Slight imperfection reads as a person. Polished symmetry reads as a
machine.

Run the audit any time you edit the pools:

```bash
python scripts/build_text_touches.py --check-pools
```

## Name hygiene

- Use the first real name token: "C Eugene Suthard" -> "Eugene". Initials only ("E A Henry") -> use the no-name variants ("hoping to reach the owner of 1100 Colonial Ave").
- Companies, trusts, and estates never get "Hi FirstName". Use owner-of-address wording.
- One name only, even for co-owner records.

## Variant pools

`{first}` owner first name, `{addr}` street line, `{city}` city, `{sender}` caller first name.

### Touch 1: identity check (send before call attempt 1)

- Hi {first}! I hope your week is going great. My name is {sender}, I was looking at {addr} and was wondering if it's yours? Thanks so much!
- Hi {first}, I pray all is well your way! I'm {sender}, and I know this is random, but does {addr} happen to be yours? Do I have the right person?
- Hey {first}, I hope you are doing great! I'm not even sure I have the right number, but is {addr} yours? Thank you! {sender}
- Hi there! I hope things are going well for you. This is {sender}, hoping to speak with {first} about {addr}. Do I have the right number?
- Hi {first}! My name is {sender}. I've been looking at {addr} in {city} and was wondering, does it belong to you by any chance? Have a great day!

No-name versions:
- Hi! I hope your week is going great. My name is {sender}, I'm trying to reach the owner of {addr}. Did I get the right number? Thanks so much!
- Hi there! This is {sender}. I know this is random, but I'm hoping to reach whoever handles {addr} in {city}. Do I have the right contact?

### Touch 2: the drip (send before call attempt 2)

- Hi {first}, I reached out the other day and wasn't sure my text went through. Is {addr} your place? {sender} here.
- Hey, sorry to bother you! Did you get my message about {addr}? Just want to make sure I have the right contact. I'm {sender}.
- Hi {first}! {sender} again. Sometimes my texts don't go through, so I wanted to try once more. Is {addr} yours?
- Hey {first}, just floating my last text back up in case it got buried. Is {addr} your property? Thanks! {sender}

No-name versions:
- Hi, {sender} here again. I texted the other day about {addr} and wasn't sure it went through. Is this the right contact for that property?
- Hey, sorry to double text! Did my message about {addr} come through? Just making sure I have the right contact. I'm {sender}.

### Touch 3: soft ask (send before call attempt 3)

- Hi {first}, {sender} again about {addr}. If it's yours, have you ever thought about selling it? No pressure at all, just curious!
- Hey {first}! I hope I'm not being a bother. I'm interested in {addr} and would love to ask you a couple quick questions. Would a short call work?
- Hi {first}, this is {sender}. I work with homeowners in {city} and I'd love to chat about {addr} for a minute or two. Would you be open to that?
- Hey {first}, me again! If you've ever considered an offer on {addr}, I'd love to be the one you talk to first. Can I give you a quick call?

No-name versions:
- Hi, {sender} again. If {addr} is one of yours, would you be open to a quick conversation about it? Happy to work around your schedule!
- Hey there, this is {sender}. I'm interested in {addr} in {city}. If you handle that property, would a short call sometime work for you?

### Touch 4: breakup (send before final call attempt)

- Hi {first}, I've sent a few texts about {addr} and haven't heard back. Did you decide to keep it instead? Either way, wishing you the best! {sender}
- Hey {first}, last one from me, I promise! If selling {addr} is ever on your mind, I'd love to be your first call. Take care! {sender}
- Hi {first}, I'll stop bugging you after this! Just wanted to leave my number in case {addr} ever becomes something you'd like to talk about. {sender}
- Hey {first}, {sender} here one more time. If I have the wrong number, I'm so sorry! If not, I'd still love to connect about {addr} whenever works for you.

No-name versions:
- Hi, {sender} here one last time about {addr}. If there's a better contact for that property, I'd be grateful for a point in the right direction. Thanks!
- Hey there, last text from me! If {addr} is ever something you'd consider selling, I'd love to be your first call. All the best! {sender}

## Seasonal and day-of-week upgrades

If you regenerate fields weekly, swap openers for timely ones (these outperform evergreen copy):

- Monday: "Happy Monday! I hope you enjoyed your weekend with loved ones..."
- Friday: "Yay it's Friday! I hope you enjoy your weekend..."
- New month: "I can't believe it's already {month}..."
- Holidays: "May you be blessed this Thanksgiving with your loved ones..."

Keep the identity-check structure; only the opener changes.

## Reply handling cheat sheet

- "Yes, who's this?" -> warm intro, one qualifying question, push for the call.
- "How did you get my number?" -> honest and calm: "I research property records for homes I'm interested in. Totally fine if it's a bad time."
- "Not interested" -> thank them, mark the record, note the tone (soft no vs hard no; soft nos become follow-ups).
- "Wrong number" -> apologize, thank them, mark the phone bad so nobody dials it.
- STOP or hostility -> DNC the number immediately, no reply.
