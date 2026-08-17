# Lead Management Call Rubric (DataSift Community Edition)

Pipeline scope: follow-up and qualification calls by the Lead Manager. The LM's job on these calls: surface the qualifying answers (condition, timeline, motivation, price), apply the 4 pillars, set the next action, and hand qualified leads to acquisitions. The LM does not present the offer, does not talk final numbers, and does not get the signature. Win condition: "a deeply qualified seller, with all four pillars surfaced and roadblocks named, handed cleanly into a booked offer call."

Scope: this rubric grades ONLY what is audible on the call itself. CRM state, custom fields, tasks, statuses, and any other Sift hygiene are out of scope and never scored here.

## How to Use (grading a transcript, 4 steps max)

1. **Gate the call.** Run the Call Applicability Gate below. If the call does not qualify for scoring, log it in the Connection Log and stop.
2. **Check auto-fails.** Scan the full transcript for any Auto-Fail Condition. If one is present, the call grade is FAIL regardless of numbers; still score the categories for coaching data and flag for manager review.
3. **Score the criteria.** Score every criterion 0 to 5 using the anchors, marking N/A only where the rubric explicitly allows it. Category score = average of its scored criteria. Call score = sum of (category score / 5) x category weight, scaled to 100. If an entire category is N/A (per the gate), redistribute its weight proportionally across the remaining categories.
4. **Write the coaching output.** Fill in the Coaching Output Format exactly: scores table, top 3 strengths with quotes, top 3 improvement areas with quotes plus the prescribed playbook line, one drill.

## Call Applicability Gate (which calls this rubric applies to)

**Score these calls:**
- Qualification calls on new or unqualified leads (the Qualifying Call and the Process Call)
- Follow-up calls on Cold, Warm, or Hot leads
- Re-qualification calls on Send Back to Lead Management records
- Ghost revival and not-interested reopen calls where the seller engages

**Do NOT score, log in the Connection Log instead** (entry: date/time, number dialed, lead age at dial for new leads when timestamps are available, disposition, and the next task set in Sift):
- Voicemails left (note whether a voicemail plus text was sent, per the full-attempt rule: a call, a voicemail, and a text)
- Wrong numbers and disconnected numbers (note the disposition so the record's primary phone gets corrected)
- Hangups or no-conversation calls with no meaningful two-way exchange (under roughly 30 seconds with no seller response)
- Gatekeeper-only contacts where the owner or decision maker was never reached

**Short calls are their own report type, never a /100 score.** If the seller declines or disengages inside roughly the first 30 seconds with no substantive exchange beyond the decline, do NOT compute a weighted total or grade band: a total built from two or three reachable criteria is not comparable with a full-call score. Write a SHORT CALL REPORT instead: score the reachable Opening and Continuity criteria (0-5 each), the no-ladder criterion 4.5 (0-5), and note whether any next action was set; then one fix and one drill, plus the SCORES JSON footer with "call_type": "short", "total": null, "band": null, "opener": [avg], "conversion": [4.5 score], "conversion_attempted": [true/false/null]. Scorecards aggregate short calls separately and never average them into full-call totals.

**Non-decision-maker calls:** if ownership/decision authority is disconfirmed early and the LM correctly stops deep qualification, score Opening and Continuity plus Next Action only. Deep-qualifying a confirmed non-decision-maker is itself a scored failure (Category 1, Criterion 3).

## Auto-Fail Conditions (an auto-fail overrides the numeric score)

Any one of these marks the call FAIL and triggers manager review. Category scores are still recorded for coaching.

Note on sourcing: conditions 1, 2, and 5 are company compliance policy additions, not standards from the training material; do not attribute them to the playbook when coaching. Conditions 3 and 4 are playbook-grounded ("Build the credibility with the truth"; the no-chasing rule).

1. **DNC violation (company compliance policy):** ignoring, arguing with, or continuing to pitch after "take me off your list" or an equivalent do-not-contact request.
2. **Abusive conduct (company compliance policy):** profanity directed at the seller, shouting, sarcasm at the seller's expense, or discriminatory remarks about the seller, their family, or their situation. (The playbook treats judgmental or negative language as a grade-dragging tonality failure, scored in 4.3; the auto-fail threshold here is a company standard.)
3. **Misrepresentation:** lying to the seller (fake buyer, fake deadline, fake "underwriting department" or partner, false statements about foreclosure, probate, or any legal process) or misrepresenting who the caller is or what company they represent. Benchmark: "Build the credibility with the truth."
4. **Harassment-territory pitching:** continuing to push after a clear, repeated, final no in the same call (the no ladder allows one structured attempt, not a siege).
5. **Privacy breach (company compliance policy):** disclosing another person's private information (for example, revealing an heir's or co-owner's financial details to a third party).

Keep this list short and objective. Tone problems, skipped questions, and process misses are scored, not auto-failed.

## Categories

Weights reflect the playbook's emphasis for this pipeline: the 4 pillars are the core job (30), the golden rule of next actions and the handoff is the win condition (20), roadblocks are "the section untrained reps skip, and it is why their deals die at the last minute" (20), rapport, control, and objection handling drive everything the pillars need (20), and continuity opens the door (10). All five categories are pure call quality.

Score each criterion 0 to 5. Category score = average of its criteria. Call score = weighted sum scaled to 100.

### Category 1: Opening and Continuity (10%)

| Criterion | What Great Looks Like (playbook example) | Common Failure | Score Anchors (0, 3, 5) |
|---|---|---|---|
| 1.1 Continuity opener, "I saw..." rule | Names the prior touch and the property from the record: "Hey [name], how's it going? This is [your name]. You actually spoke to [Cold Caller's name] about selling [address]." Caller's name pulled from the activity log. | "Just checking in" (gives the seller "absolutely nothing meaningful to respond to"), or "So, tell me about your situation" when the CRM already has it. | 0 = opens cold, makes the seller repeat themselves; 3 = references the property or a prior contact vaguely, some re-asking of known facts; 5 = names the person, address, and prior conversation, zero repeated questions. |
| 1.2 Expectation setting at the open | Time, purpose, and outcome stated up front: "Do you have 15 minutes real quick for us to cover a few things so we can get you over an offer?" plus the permission line: "if at any point you feel like this isn't the right fit for you, that's perfectly fine. You won't hurt my feelings." | Launching into questions with no time ask, no stated purpose, and no exit offered; the seller's guard stays up. | 0 = no time, agenda, or result stated; 3 = time and purpose stated but no permission/exit line; 5 = time, agenda, result, and the exit line all present at the open. |
| 1.3 Decision-maker confirmation | "And just to confirm, you're the owner or the one who can make the decision on this, right?" The playbook calls this non-negotiable. | Deep-qualifying a non-decision-maker; the whole call's data is unusable and the real owner is untouched. | 0 = never confirmed and proceeded to deep-qualify; 3 = confirmed late or implicitly; 5 = confirmed early and explicitly before deep qualification. |
| 1.4 New-information re-contact (N/A on first qualification call) | Each touch carries the seller's specific situation or clock: "Hey so-and-so, I know you're still going to auction on [X date], I'm just calling to see if there's anything that we can do to help." Soft reopen: "I was going through my files and didn't see if you ever sold that property. I wanted to update my notes," then go quiet. | Generic drip-sounding touch that forces the seller to re-explain; robotic "following up on my last call." | 0 = generic touch, no reference to their situation; 3 = references the situation but adds nothing new; 5 = leads with their specific clock or fact, then silence. |
| 1.5 Revival toolset and ghost diagnosis (N/A unless a revival or reopen call) | Diagnoses every ghost as an uncovered objection and classifies the cause as Price, Time, Third Party, or Trust. Deploys the shipped tools by stage: the soft reopen (1.4), the permission-to-close ask ("Did you want me to stop reaching out?"), the "close your file" breakup, and the 9-word text. | Generic re-pitching at a ghost with no diagnosis of why they went dark; jumping straight to a breakup with no permission ask; the ghost cause never named. | 0 = no revival tool used and no cause diagnosed; 3 = a tool deployed but the ghost cause never classified as Price, Time, Third Party, or Trust; 5 = tool matched to the ghost's stage, cause classified as Price, Time, Third Party, or Trust, and the uncovered objection worked or the file closed gracefully. |

### Category 2: Four Pillars Qualification (30%)

| Criterion | What Great Looks Like (playbook example) | Common Failure | Score Anchors (0, 3, 5) |
|---|---|---|---|
| 2.1 Motivation: the reason behind the trigger, layered | "So you're currently in foreclosure. For me to best help you, I need to ask a few questions. Is the mortgage payment too high, or what kind of got us into this situation?" Then peels layers: "What made you consider selling this property now?", "What's your picture-perfect scenario?", "How does that make you feel?" Playbook rule: "Foreclosure is not a reason for selling. It's a byproduct of a reasoning." | Accepting the list trigger or "financial reasons" at face value and jumping to price; yes/no motivation questions ("Are you motivated to sell?"). | 0 = never asked why, or took the trigger as the reason; 3 = asked once with decent phrasing, accepted the first answer; 5 = asked openly, peeled at least one layer (Problem, Picture-Perfect, or Impact), captured the driver in the seller's own words. |
| 2.2 Timeline to agreement, bucketed | "Before we get started, so I can understand, because we reserve our funds right away, what timeline are you actually looking to go under agreement with somebody?" Captured as ASAP, 1 to 3 months, 3 to 6 months, or 6 months plus. "going under agreement is a lot different answer than when do you want to actually close on the home." | Asking closing timeline instead of agreement timeline; skipping timeline; hearing "6 months out" and keeping the lead hot anyway (or the reverse). | 0 = timeline never asked; 3 = asked but not bucketed, or answer not used to set temperature; 5 = agreement-timeline asked, bucketed, and reflected in the next-action cadence. |
| 2.3 Condition: three-bucket grade, tour, capex | "Would you say it's in excellent shape, needs a little updating or minor cosmetics, or needs a lot of work?" then "Walk me through it like a tour, starting at the front door." Capex: "When was the roof last replaced? How old is the HVAC and the water heater? Is the electrical on breakers or fuses?" Seeds cost aloud without committing: "That already sounds like at least 30k." | Interrogation-style checklist ("How many bedrooms? How many baths?" fired in sequence); criticizing the house; skipping capex entirely. | 0 = condition never covered or reduced to bed/bath count; 3 = bucket question asked but no tour or no capex items; 5 = bucket, tour, and at least two capex items, with the seller doing most of the talking. |
| 2.4 Price: pocket number pulled, reflected, never negotiated | "Do you have a number in mind for what you're trying to walk away with in your pocket?" then reflects it back: "So it sounds like you're trying to get about [X] in your pocket." Plus: "What are you hoping to get for it, and how firm is that? Have you gotten any other offers, or listed it with an agent in the last 90 days or 6 months?" Accept the number, do not negotiate; negotiation belongs to the Closer. | Giving a number first ("sometimes you'll out negotiate yourself"), negotiating the seller's number on this call, or skipping firmness, competing offers, and agent status. | 0 = no price conversation, or LM quoted or negotiated a number; 3 = asked for a number but no reflection, no firmness or offers/agent check; 5 = pocket number pulled and mirrored back, firmness plus offers plus agent history covered, zero negotiation. |
| 2.5 Pillar order and flow discipline | Motivation before condition: "Asking about the roof and furnace early reads as an appraisal and puts the seller on guard about price." The Qualifying Call sequence itself asks timeline early and the pocket number before condition; that order is correct, not a violation ("price last" applies to the Process Call money re-ask, which closes that call). When the seller volunteers a pillar out of order, take the answer and keep flowing: "cross off whichever MCTP letter they answer and keep flowing." | Script-order rigidity (seller volunteers timeline and the rep drags them back to the script), or opening the call with roof and furnace questions. | 0 = condition led the call before motivation was touched, or volunteered pillars ignored; 3 = mostly correct order with some rigidity or one early condition probe; 5 = motivation surfaced before condition, volunteered answers absorbed naturally, and (on a Process Call) the money re-ask held for last. |

### Category 3: Roadblocks and Logistics (20%)

| Criterion | What Great Looks Like (playbook example) | Common Failure | Score Anchors (0, 3, 5) |
|---|---|---|---|
| 3.1 Influencers | "Would anyone else be upset if you made a decision about this property without speaking with them first?" | The spouse or sibling objection surfaces for the first time at the Closer's offer, killing the deal; "the section untrained reps skip." | 0 = never asked; 3 = asked as a closed checkbox with no follow-up on the answer; 5 = asked openly and any named influencer logged as a roadblock with a plan. |
| 3.2 Logistics and after the sale | "Let's assume you like our offer and you're happy with the terms. What's the next thing you would have to figure out, if anything, to make this happen?" and "And what are you doing after we sell the property?" (say "we sell," not "we buy"). | The named failure: the elderly seller who agrees to a 7-day close and cannot physically move out. Skipping this question entirely. | 0 = neither question asked; 3 = one of the two asked, answer not probed; 5 = both asked, relocation and time-in-property needs surfaced and noted. |
| 3.3 Encumbrances, title, and agent exposure | "Is there still a mortgage on it, and roughly what's the balance? Are you behind on any payments or taxes? Any HOA, code violations, or liens?" Plus agent history ("listed it with an agent in the last 90 days or 6 months?"), foreclosure status, whether probate has been filed, other heirs. | Blind handoff: the Closer discovers the second mortgage, the lien, or the agent commission entitlement after the offer is out. | 0 = no encumbrance questions; 3 = mortgage asked but taxes, liens, or agent history missed; 5 = mortgage balance, arrears, liens or HOA, and agent history all covered (plus probate/heirs where the list calls for it). |
| 3.4 Occupancy and junk | "Is anyone living there right now, is it vacant, or is there a tenant on a lease? And how much stuff is left in it, minor or significant?" Cross-check Google Street View live on the call. | Tenant on a lease, a house full of junk, or a titleless vehicle discovered at the walkthrough instead of on this call. | 0 = occupancy never asked; 3 = occupancy asked but contents/junk skipped; 5 = occupancy, lease status, and contents all captured. |

### Category 4: Rapport, Tonality, and Call Control (20%)

| Criterion | What Great Looks Like (playbook example) | Common Failure | Score Anchors (0, 3, 5) |
|---|---|---|---|
| 4.1 Question, then silence | Asks the question and stops: "After any of these, go quiet and let the silence do the work." The soft reopen ends with "then go quiet." | Filling every pause, answering your own question, cutting the seller off mid-reveal, talking past the reveal. | 0 = repeatedly answers own questions or talks over answers; 3 = mostly waits but steps on one or two key reveals; 5 = clean silences after the big questions, seller carries the longest turns. |
| 4.2 Mirroring and reflection | "When you say [their last few words], what do you mean by that?" and reflected summaries: "So, if I understand correctly, the tenants haven't been paying rent consistently." Reflects the pocket number back: "So it sounds like you're trying to get about [X] in your pocket." | Robotic acknowledgments ("gotcha... gotcha... gotcha") with no content reflected; moving to the next scripted question regardless of the answer. | 0 = zero mirrors or reflections all call; 3 = one or two reflections, some answers unacknowledged; 5 = mirrors and labeled summaries at each major disclosure, seller audibly feels heard. |
| 4.3 Empathy beats and non-judgment | On a loss: brief, sincere, then advance: "Well, first and foremost, I'm sorry to hear about your mom's passing." Never names the distress: "Hey, I was just calling about 123 Main Street, to see if you had any plans for it." De-judgment line available: "I want you to know upfront that I'm not here to judge anyone's situation. Life happens." | Skipping the condolence, wallowing in it and never advancing, or naming the distress ("I saw your foreclosure notice") which reads as surveillance and judgment. Negative remarks about the house. | 0 = distress or loss disclosed and ignored, or distress named judgmentally; 3 = acknowledgment present but rushed or slightly off register; 5 = brief sincere beat, register matched to the avatar, then business advanced. |
| 4.4 Let them rant, keep control | On the condition tour: "the more that they talk about it, the more emotionally they get... the more that I can hopefully catch them slipping." Sellers are perpetual optimists, "so you let them talk but you control the conversation." The 80/20 rule: mostly ears, 20% directed guided questions. | Lecturing, over-educating the seller early (ARV lectures), interrogating, or losing the call to a 15-minute perfect-house monologue with no steer (miss the "sounds like a perfect house, why are you even considering selling it?" disrupt). | 0 = LM dominates or completely loses the wheel; 3 = decent balance but drifts or lectures once; 5 = seller talks most, LM steers every segment back to a pillar or roadblock. |
| 4.5 The no ladder and objection handling | Never accepts a first no flat: "Ok, so you are not interested in a cash offer for the property?" then "So what has changed?" then the referral ask, then a graceful exit. Converting a no: "is that no now or just no never?" | "Okay, thanks, bye" on the first no; or the opposite failure, arguing with the no and chasing (desperate energy, begging). | 0 = first no accepted flat or argued with; 3 = one rebuttal attempt but no diagnosis of what changed; 5 = full ladder run calmly, no boxed as now-vs-never, graceful exit with the door open. |

### Category 5: Next Action and Handoff (20%)

| Criterion | What Great Looks Like (playbook example) | Common Failure | Score Anchors (0, 3, 5) |
|---|---|---|---|
| 5.1 Binary outcome rule | Call ends with "either an appointment set for the acquisition specialist or a confirmed follow-up task with a date. No lead should ever sit without a next action and there is no third option." | The vague ending: "we'll be in touch," "I'll follow up with you sometime." A lead with no task is a dying lead. | 0 = call ends with no next action of any kind; 3 = a next step exists but only one-sided ("I'll try you next week") with no seller confirmation; 5 = appointment booked or a follow-up confirmed with the seller, with a date. |
| 5.2 Specific next step with expectations set | "I can give you a call back around 4 or 5 o'clock and I pretty much just want to take 5 to 10 minutes of your time, let you know what it's like to work with us and let you know what the property can qualify for." Time, agenda, result stated before hanging up. | "An open 'sometime next week' is a ghost waiting to happen." Ghosting "is the predictable result of skipping the expectation-setting at the end of the prior call." | 0 = no time set; 3 = day set but no time, or time set with no agenda/result; 5 = specific date and time plus what the next call covers and what it decides. |
| 5.3 Cadence match to pillar evidence | Temperature is set off the pillars: Hot = two or more Hot pillars, clear motivation, short timeline; Warm = one Hot pillar; Cold = zero. Cadence matched: Hot every 1 to 2 days, Warm every 15 days, Cold every 45, foreclosure tightening hard near auction. Not daily on Hot: "if you're calling every day, you kind of get the commission breath." Grade the booked follow-up gap against the cadence the pillar evidence on this call supports. | A "6 months out" seller kept in the hot pipeline; a "yesterday" seller dropped into a 45-day drip; drip-cadence promises to hot and warm sellers. | 0 = booked follow-up gap contradicts the pillar evidence on the call (a "yesterday" seller pushed to a 45-day touch, or daily calling promised on a Hot lead); 3 = gap roughly right but off the cadence table by one step, or inside the auction clock with no tightening; 5 = next touch lands on the correct cadence for the pillars proved on the call (and inside the auction clock where relevant). |
| 5.4 Handoff readiness and warm transfer (N/A if the lead is not yet qualified) | Before handoff the call has produced: confirmed name and best number, property address, motivation in the seller's own words, timeline, real condition answers, all four pillars surfaced, every roadblock named, and the booked offer-call time. Warm bridge when live: "It really just depends on the condition of the property... let me get my partner on the line... I think they are available right now." | Handing off with pillars missing; the LM sliding into Closer territory (presenting the offer, talking final numbers, negotiating the pocket number). | 0 = handoff attempted with two or more pillars missing, or LM negotiated numbers; 3 = handoff data mostly complete but no booked offer-call time or one roadblock unexplored; 5 = complete package, roadblocks named, offer call booked or warm transfer executed while the seller was engaged. |
| 5.5 Human details drawn out for the next touch | The call draws out the ammunition the next caller needs: "She moved to Florida, doesn't even live here anymore, is living off Social Security." The situational and emotional specifics get surfaced in conversation, not left as vague impressions. | A pleasant conversation that surfaces nothing specific; the seller's real situation stays fuzzy and the next touch has nothing to anchor on. | 0 = no personal or situational details surfaced on the call; 3 = a few facts surfaced, key emotional drivers missing; 5 = motivation in the seller's own words plus the human details (family, move plans, money pressures) drawn out on the call. |

## Grade Bands

| Band | Score | Coaching Directive |
|---|---|---|
| Elite | 90-100 | Use this call as training tape for the team and have the LM lead the next role-play session. |
| Strong | 75-89 | Pick the single lowest category and run one targeted drill on it this week; everything else is maintenance. |
| Developing | 60-74 | Re-drill the Qualifying Call sequence and end-of-call expectation setting end-to-end in role-play twice this week until pillar order is automatic. |
| Needs Work | 40-59 | Return to script adherence: LM shadows an elite call recording daily and role-plays the full sequence with a manager before the next live block. |
| Retrain | Below 40 | Pull from unsupervised live leads and restart the Lead Manager Playbook training with daily role-play sign-off before requalifying for the queue. |

An auto-fail call reports as FAIL, not a band, with category scores attached for the manager review.

## Tonality Evaluation Guide (transcript-observable proxies only)

Grade tonality from what the transcript shows, never from a gut feeling about "sounding bad."

- **Talk-listen balance (inferred from line lengths):** apply the 80/20 rule: "80% of the time you're using these two things God gave you, 20% of the time you're asking very directed guided questions." In the motivation and condition sections the seller should own the longest turns; a follow-up open can run rep-heavier because the LM must carry the re-entry. Flag any LM monologue longer than the seller's longest story.
- **Question count and mix:** count open-ended vs closed; the pillars should be opened with open-ended phrasing ("what kind of got us into this situation") not yes/no checkboxes. A rapid-fire run of stacked closed questions reads as interrogation regardless of the total count.
- **Silence honored:** after a pillar question, the next transcript line should be the seller. Count instances of the LM answering their own question or stacking a second question before the seller responds. Zero is the target.
- **Interruptions:** count talk-overs if the transcript marks them (crosstalk, cut-off turns). Zero ideal, two or more drags the score.
- **Filler words:** rate of "um, uh, like, you know" per LM turn. "It's better just to not say something than to say um." Score as a rate, not an absolute count.
- **Mirrors and labels:** count occurrences of repeating the seller's last few words as a question and "it sounds like / it seems like" labels. At least one per major disclosure is the benchmark; the pocket-number reflection ("So it sounds like you're trying to get about [X] in your pocket") counts.
- **Empathy beats:** on any disclosed loss or distress, look for a brief sincere acknowledgment followed by forward motion within one or two turns. Both the missing beat and the wallowing beat are failures.
- **Phrasing tells of a top performer:** the seller's name used naturally, softeners ("just kind of curious," "no worries"), "we sell" not "we buy," "us" framing, same-team language, zero fake urgency, zero fake departments or partners.
- **Script recital tells:** identical stock phrasing regardless of the seller's answers, questions asked that the seller already answered, no absorption of volunteered pillars. Grade for internalized framework, not line recital.
- **Audio cues, only if present in the transcript:** if the transcription carries delivery notes (pauses, laughter, pace or tone annotations), use them: slow down and lower your energy slightly at the open, calm unhurried delivery over speed, and getting quieter when the seller gets louder ("great callers get quieter when the seller gets louder"). Never infer audio qualities the transcript does not show.

## Perfect Call Definition

A 100 call opens with continuity and permission: the LM names the seller, the address, and the prior touch pulled from the activity log ("You actually spoke to [Cold Caller's name] about selling [address]"), sets time, agenda, and result, and hands the seller an exit ("You won't hurt my feelings"). Ownership and decision authority are confirmed before anything deep. The four pillars are then surfaced per the Qualifying Call sequence and in the seller's own words: timeline to agreement bucketed early, the reason behind the trigger peeled past the first answer, the pocket number pulled, mirrored back, and accepted without a hint of negotiation, and condition graded three ways then toured from the front door with capex ballparked aloud. Motivation is on the table before any condition probe, and any pillar the seller volunteers out of order is absorbed on the spot rather than dragged back to the script. The roadblocks section is not skipped: influencers, after-the-sale logistics, encumbrances and agent history, occupancy and junk all get asked, and the seller does most of the talking while the LM steers with silence, mirrors, and brief sincere empathy. The call ends with the binary outcome honored: either a booked offer call for acquisitions with the complete handoff package (all four pillar scores, roadblocks named, best number confirmed) or a confirmed follow-up with a specific date, time, agenda, and result, at a cadence that matches the temperature the pillars just proved.

## Coaching Output Format

The grader fills this template exactly, for every scored call:

```
CALL GRADE REPORT: Lead Management
Call ID / Recording: [id or link]
Lead Manager: [name]
Seller / Property: [name, address]
Call Type: [qualification | follow-up | re-qualification | revival]
Date: [date]    Duration: [mm:ss]
Applicability: [Scored | Connection Log | Prorated short-call]
Auto-Fail: [NONE | condition # and quote]

SCORES
| Category                            | Weight | Score (0-5) | Weighted |
|-------------------------------------|--------|-------------|----------|
| 1. Opening and Continuity           | 10%    |             |          |
| 2. Four Pillars Qualification       | 30%    |             |          |
| 3. Roadblocks and Logistics         | 20%    |             |          |
| 4. Rapport, Tonality, Call Control  | 20%    |             |          |
| 5. Next Action and Handoff          | 20%    |             |          |
| TOTAL                               | 100%   |             | /100     |
Grade Band: [Elite | Strong | Developing | Needs Work | Retrain | FAIL]

CRITERION SCORES (required: one table row per rubric criterion, in rubric order)
| Criterion | Score (0-5 or N/A) |
|---|---|
| [number and short name, e.g. "2.1 Motivation: the reason behind the trigger"] | |

PILLAR STATUS (from this call)
Motivation: [captured value or MISSING]   Timeline: [bucket or MISSING]
Condition: [bucket or MISSING]            Price: [pocket number or MISSING]
Temperature the pillar evidence supports: [Hot | Warm | Cold]
Next action booked: [date/time or NONE]

TOP 3 STRENGTHS (verbatim quotes from the transcript)
1. "[quote]" : [why it worked, tied to a rubric criterion]
2. "[quote]" : [why it worked]
3. "[quote]" : [why it worked]

TOP 3 IMPROVEMENT AREAS (quote, then the prescribed playbook line)
1. What happened: "[quote from transcript]"
   Use instead: "[exact playbook line from this rubric]"
2. What happened: "[quote]"
   Use instead: "[playbook line]"
3. What happened: "[quote]"
   Use instead: "[playbook line]"

ONE DRILL
[Single practice assignment targeting the lowest-scoring criterion, with reps and
a due date. Example: 10 role-play reps of the pocket-number ask plus reflection,
"Do you have a number in mind for what you're trying to walk away with in your
pocket?" then mirror and go silent, before Friday's call block.]
```

### SCORES JSON (required on EVERY report, full and short)

The last block of every report is one fenced JSON object whose numbers match the
tables above exactly (null for N/A, no comments, no trailing commas):

```json
{"call_id": "...", "caller": "...", "call_type": "full", "duration_seconds": 0,
 "outcome": "...", "auto_fail": "PASS", "total": 0.0, "band": "...",
 "categories": {"1": 0.0, "2": 0.0, "3": 0.0, "4": 0.0, "5": 0.0},
 "criteria": {"1.1": 0, "...": 0}, "recording_url": "..."}
```

Short calls: "call_type": "short", "total": null, "band": null, plus
"opener", "conversion", "conversion_attempted" as defined in the gate section.