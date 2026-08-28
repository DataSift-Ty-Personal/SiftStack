# Walkthrough to offer

How a folder of walk videos becomes one number you can say out loud to a seller.

Worked end to end on **1342 Grainger Ave, Knoxville TN 37917** on 2026-08-27, which
is the example used throughout. That run produced an offer of **$93,920**.

The agent-executable twin of this document is `sops/walkthrough-to-offer.sop.md`,
served over MCP as `/agent-sops:walkthrough-to-offer`.

---

## The chain

```
walk videos  ->  frames  ->  condition
county card  ->  subject facts (beds, baths, sqft, year)
drawn map    ->  polygon  ->  comps  ->  ARV
comp listings ->  finish recipe  ->  ONE rehab number
                                        |
                                        v
                         percent-of-ARV rule  ->  OFFER
```

Five tabs come out: **Offer**, **The House**, **Repair Detail**, **Comps**, **Buyers**.
The Offer page is the deliverable. The other four exist so the offer is auditable.

---

## 1. Frames, not narration

```bash
ffprobe -v error -show_entries format=duration -show_entries stream=codec_type <video>
ffmpeg -v error -i <video> -vf "fps=1/2,scale=900:-1" -q:v 4 frames/i_%03d.jpg
ffmpeg -v error -i <still>.HEIC -map 0:v:0 -frames:v 1 -q:v 3 still.jpg
```

Roughly 190 frames for 6 minutes of video. Read them in batches.

**Transcribe the audio only if there is speech worth having.** On Grainger both videos
had audio, so they were transcribed for about two cents, and both were ambient
conversation with the seller rather than scoping narration. The interior transcript
produced nothing. Two facts did survive, and both mattered: a comment about a "second
unit back here" (the attached rear wing, resolved from frames) and confirmation that
the AC unit runs.

**The frames are the evidence.** Zoom into anything ambiguous rather than guessing:

```bash
ffmpeg -v error -ss 48 -i <video> -vf "crop=760:1080:200:0" -frames:v 1 -q:v 2 zoom.jpg
```

That is how the hallway debris on Grainger was identified as a failed wall cavity
rather than a ceiling collapse.

## 2. The county card wins

Query the parcel, then the assessor card:

```
https://knox-tn.mygovonline.com/api/v2/parcels/<query>?detailLevel=public&start=0&length=25
https://propertyinfo.knoxcountytn.gov/Datalets/Datalet.aspx?mode=residential&UseSearch=no&pin=<PARID>
```

Grainger came back 4 bed / 1 full bath / 0 half, 1,332 sf (888 main plus 444 upper
story finished), built 1920, exterior wall code 04, quality 55 GOOD. Zillow agreed on
the headline numbers.

**Watch the fixture count.** The card recorded one full bath and **six total
fixtures**, and the frames showed a toilet and sink upstairs that the county does not
count. That uncounted plumbing is what made a legal half bath cheap, and it is a
common find in old houses.

## 3. Draw the boundary, then prove it

A pocket is bounded by roads, highways and rail. Use `--polygon`, never `--bbox`.
Trace the drawn map into `[lat, lon]` pairs, then **print every street that landed
inside** and compare against the map.

On Grainger the polygon was right and it was doing real work: it excluded the Old
North Knoxville Victorian corridor on Luttrell St that sold **$690,000 to $1,350,000**,
which sits south of I-40 and outside the line. Inside it were 27 sold comps on
Grainger, Hoitt, Leonard, Overton, Cecil, N 4th through N 6th, Brown and Thompson.

## 4. Judge the ARV, do not accept it

```bash
python src/post_walkthrough.py --address "1342 Grainger Ave" --city Knoxville --zip 37917 \
    --beds 4 --baths 1 --sqft 1332 --year-built 1920 --months 24 \
    --polygon output/1342_grainger_polygon.json \
    --walkthrough walk_1342_grainger.json \
    --save-pack output/1342_grainger_pack.json
```

`--months` is the POOL, not the preference. `tight_arv` prefers 12 months and widens
only when that leaves under three comps, so passing a narrow pool starves the widener.

**The engine returned $481,000 and it was wrong.** Its same-bed clamp left three
renovated three-bed sales, and two of them were 1,600 to 1,660 sf homes on Leonard Pl
and Luttrell St, the two most expensive streets inside the boundary. The sanity check
killed it: 1318 Grainger, two doors down, sold at $274/sf.

Releasing the bed clamp to size peers gave 11 comps at $286/sf. Then the bath split:

| | median $/sf | at 1,332 sf |
|---|---|---|
| 1 bath (n=4) | $276 | $367,000 |
| 2+ bath (n=7) | $299 | $398,000 |

An 8.6% bath premium, measured rather than assumed. The final figure was set at
**$348,000 ($261/sf)**, the bottom of the peer band, which sits under even the one-bath
read.

Boundary variants were tested at 0.20, 0.25 and 0.30 miles and a trimmed polygon. All
hit the same two or three comps, which proved the constraint was the bed clamp and not
the boundary. Test the boundary anyway; that is how you find out which it is.

## 5. Let the comps pick the finish

Pull `property-details-address` for the closest peers and read the descriptions.

- **1410 Cecil, $276/sf:** "original unpainted trim, doors, **windows**, and hardwood
  floors, all preserved." Kitchen remodeled, bathroom "stylishly refreshed."
- **1910 Luttrell, $292/sf:** "original clawfoot tub," original hardwood, Victorian
  doors.
- **1108 Overton, $299/sf, top of the band:** a **1.5 bath** with laminate floors and a
  granite kitchen.

That pocket pays for preserved character plus a new kitchen. It does not pay for a gut
with LVP and vinyl windows. So the scope became a renovation with `gut: false`, the
Windows and HVAC categories dropped, and the Overton comp is the whole reason the
target stayed 4 bed / 1.5 bath instead of losing a bedroom.

Write the evidence into `comp_finish_basis`, quoting the comps by name and price.

## 6. One rehab number

Set `single_scenario` in the walk file. Never emit the four-scenario matrix.

```json
"single_scenario": {
  "key": "reno", "label": "Comp-Match Reno (4 bed / 1.5 bath)",
  "tier": 2, "scope": "full", "gut": false,
  "beds": 4, "baths": 1.5, "drop": ["Windows", "HVAC"]
}
```

Grainger landed at **$134,680**, $101/sf, 7.8 weeks. Walk flags were $69,400 of it,
52%, which is the honest story of that house: porch, siding, foundation, water damage
and estate cleanout are condition, not finish.

## 7. Render and verify

```bash
python src/offer_sheet.py --pack output/1342_grainger_pack.json \
    --walk walk_1342_grainger.json --out "1342_Grainger_Ave_Offer.xlsx"
```

Then compute it the way Excel would, rather than reading it:

```python
import formulas
sol = formulas.ExcelModel().loads("1342_Grainger_Ave_Offer.xlsx").finish().calculate()
```

Assert the offer and assert zero formula errors. Then flip `RulePct` from 0.70 to 0.75
and confirm the offer, the buyer profit and every return move together. On Grainger
that moved the offer from $93,920 to $111,320 and dropped buyer profit from $79,376 to
$61,976, which proves the page is live.

## 8. Say the assignment math out loud

```
Buyer's maximum   0.70 x $348,000 - $134,680  =  $108,920
Our fee                                          $ 15,000
OFFER TO SELLER                                  $ 93,920
```

To clear a $15,000 fee, buy at $93,900 or under. To clear $25,000, buy at $83,900 or
under.

**Say it when the numbers do not work.** The engine's own wholesale lane suggested
assigning at $182,000 for a $67,000 fee, derived from the as-is comp band. A flipper
carrying a $134,680 rehab cannot pay that; the gap is $73,000. The as-is band reflects
investor buys on houses needing far less work. Report the conflict rather than
shipping the flattering number.

---

## The four traps, all of which fail silently

**1. Flag scenario keys.** `single_scenario` sets the key to `reno`. A flag still
carrying `["mid","gut_t2","gut_t3"]` is dropped with no warning. On Grainger that would
have deleted **$19,900** of real work: foundation repointing, basement moisture, the
carport and final clean. **Set every flag's `scenarios` to `[]`.**

**2. The stale pack.** A `--save-pack` JSON is a snapshot and does not track later
edits. Grainger's pack held both the old four-scenario rehab totals and the old
$481,000 ARV long after both were corrected, and a naive re-render produced a $187,020
offer. Recompute rehab from the walk file, let explicit CLI values beat the pack, and
write corrections back into the pack.

**3. The bed clamp.** `tight_arv` matches bed count exactly. In a thin pocket that can
leave three comps sitting on the two most expensive streets. Inspect which comps built
the number before believing it.

**4. Autofit and column widths.** `_autofit` sizes off the longest string per column,
and full-width paragraphs live in column A, which blew the offer page to 221 width
units and scrolled sideways. **Set widths after `_polish`, then re-height the wrapped
rows** against the width they actually get.

## Smaller things that cost time

- Excel holds an exclusive lock. If the workbook is open, `wb.save()` raises
  `PermissionError`; the renderer writes a `_PENDING_` copy instead.
- `output/`, `*.xlsx` and `walk_*.json` are gitignored. Deal artifacts stay local by
  design; only code and docs are committed.
- The locked material list is pinned to zip 37914. It is Knox-local and correct for any
  Knox deal, but say so rather than letting someone discover it.
- The CRM is worth reading before you trust the walk. On Grainger it revealed the owner
  had died, probate was never opened, the house was vacant, and the seller had already
  named the defect ("plumbing came apart in the bathroom"). That corrected two
  conclusions drawn from the video alone.
