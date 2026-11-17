# Step Guide Template (Scribe Style)

A step guide is a click-path document: one screen action per step, one screenshot per step, written the way Scribe auto-generates guides. Use it when the whole process lives inside software and the reader just needs to follow the clicks. No metaphors, no strategy, no long prose. If the process has real decision logic or work outside the screen, use an SOP instead.

---

## The Format

```markdown
# [Guide Title]

[N] steps · Created [Month D, YYYY] · Source: [app or site name]

**When to use:** [One sentence.]
**Before you start:** [Logins or state needed. Omit if none.]

---

## Step 01: [Action-verb sentence]

> **SCREENSHOT: [Short label]**
>
> *Capture: [What screen, in what state]*
> *Highlight: [The exact element to box or circle]*
> *Crop: [How tight to zoom, with enough context to orient]*

[Optional: one supporting line ONLY if the action needs a warning or a value to enter.]

## Step 02: [Action-verb sentence]

...

---

**Done when:** [What the reader sees when it worked.]
```

## Format Rules

1. **Title under 60 characters.** Name the app and the task: "Upload a CSV in DataSift", "Create a Filter Preset in Sift". Specific beats generic.
2. **Metadata line right under the title:** step count, created date, source app, joined with a middle dot (`·`). Then a horizontal rule.
3. **Step numbers zero-padded to 2 digits.** `## Step 01:`, never `## Step 1:`.
4. **The step heading IS the instruction.** One sentence, starts with an action verb, names the exact target the reader sees on screen. No heading like "Exporting" with the instruction buried in a paragraph below.
5. **One action per step.** "Click Save, then click Confirm" is two steps. The test: one step = one screenshot could capture it.
6. **One screenshot spec per step**, directly under the heading. Every step gets one. A step with nothing to show does not belong in a step guide.
7. **Body text is rare.** Most steps are heading plus screenshot and nothing else. Add a line only for a warning, a value to type, or a wait ("processing takes about a minute").
8. **Close with "Done when:"** so the reader can verify the run worked.

## Action-Verb Phrasing

The verb comes from what the reader does, and the target is named by its visible label:

| Action | Phrasing | Example |
|--------|----------|---------|
| Click a button | Click the [label] button | Click the **Finish Upload** button |
| Click a link | Click the [label] link | Click the **Records** link in the sidebar |
| Type into a field | Enter [what] in the [label] field | Enter the list name in the **List Name** field |
| Pick from a dropdown | Select [option] from the [label] dropdown | Select **Knox** from the **County** dropdown |
| Check/toggle | Toggle [label] on/off | Toggle **Do not replace owners** off |
| Keyboard | Press [key] | Press **Enter** to search |
| Go somewhere | Navigate to [page] | Navigate to the **Sequences** page |
| Upload | Set [file] on the [label] uploader | Set your CSV on the **Upload File** area |

Rules:
- Use the label exactly as the UI shows it, in **bold**. If the UI says "Finish Upload", do not write "Submit".
- Name the location when the element is easy to miss: "in the top-right corner", "at the bottom of the filter panel".
- Merge typing into ONE step per field. "Type each letter" noise is never steps; "Enter the full address in the Search field" is one step.
- Consecutive identical actions collapse: filling five fields on one form can be one step per field, or one step with the values listed, whichever reads faster. Never one step per keystroke or per obvious substep.

## When the Screenshots Already Exist

If the shots are already captured (a recording tool's markdown export, or screenshots taken by an agent driving the browser), skip the spec blockquote and reference the image directly under the step heading:

```markdown
## Step 03: Click the Export button

![Step 03](images/step03.png)
```

Local paths and base64 data URIs both embed into the Word doc automatically, with the alt text as the caption. Before embedding a capture-tool export, still apply the format rules: retitle steps to the action-verb phrasing, merge keystroke noise into one step per field, and confirm anything sensitive in the images is blurred.

## Screenshot Spec (what the capture person does)

Each spec mirrors how Scribe annotates:

- **Capture:** the full screen or window, in the state the reader will see at that moment. Take it BEFORE the click for actions, AFTER for results.
- **Highlight:** exactly one element per shot, boxed or circled. That element is the one named in the step heading.
- **Crop:** zoom to the action area with roughly a third of the shot as surrounding context so the reader can orient. Do not crop so tight the reader cannot tell what screen they are on.

Add a final result screenshot at "Done when:" showing the success state.

**Redact before sharing:** blur emails, phone numbers, addresses, and any customer data visible in the capture. A guide gets forwarded; assume everyone will see it.

## When a Step Guide Grows Branches

A step guide handles at most ONE simple fork, written as two labeled step runs:

```markdown
## Step 06: Choose your upload type

> [screenshot spec]

**If this is a new list:** continue to Step 07.
**If you are adding to an existing list:** skip to Step 10.
```

More branching than that means the process is not a click path. Promote it to an SOP and keep step-guide styling inside each linear stretch.

## Length

A step guide is 5 to 25 steps. Under 5, it is a sentence in someone else's doc. Over 25, split it into guides per phase ("Part 1: Upload", "Part 2: Mapping") and link them.
