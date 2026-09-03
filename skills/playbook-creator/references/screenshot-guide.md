# Screenshot Guide (Scribe-Style Capture Specs)

All visuals are **actual screenshots of software interfaces**, never custom illustrations or generic graphics. The documents this skill produces contain screenshot SPECS: precise instructions for whoever captures the shots. A good spec reads like a Scribe step: it names the screen, the one element to highlight, and how tight to crop.

---

## The Spec Format

Every screenshot placeholder uses this blockquote format (the docx builder styles it as a yellow callout):

```markdown
> **SCREENSHOT: [Short label]**
>
> *Capture: [What screen or window, in what state]*
> *Highlight: [The ONE element to box or circle]*
> *Crop: [How tight to zoom, keeping enough context to orient]*
```

The three lines mirror how Scribe annotates automatically:

| Line | What it controls | Rule |
|------|------------------|------|
| **Capture** | The raw shot | Full screen or window, in the exact state the reader will see. Before the click for actions, after the click for results. |
| **Highlight** | The annotation | Exactly ONE element per shot, boxed or circled. It is the element named in the step's action sentence. |
| **Crop** | The zoom | Center on the highlighted element with roughly a third of the frame as surrounding context. Never so tight the reader cannot tell what screen they are on. |

**Good spec:**
```markdown
> **SCREENSHOT: Column mapping, Tags column**
>
> *Capture: DataSift Upload Wizard step 4, with Property Street already auto-mapped (green check)*
> *Highlight: The Tags column's drag-and-drop area*
> *Crop: Zoom to the mapping table; keep the wizard step bar visible at the top*
```

**Bad spec:**
```markdown
> **SCREENSHOT: Upload screen**
>
> *Capture: The screen*
> *Highlight: The button*
> *Crop: Whatever fits*
```

---

## Placement

1. **Actions:** spec goes right under the action it illustrates. In a step guide, directly under the step heading. In an SOP, after the numbered action line it shows.
2. **Results:** after the step, showing the success state the reader should compare against.
3. One blank line before and after the blockquote.

## When to Add a Screenshot

### Always

| Situation | Example |
|-----------|---------|
| First time showing a tool | Main dashboard, so the reader knows the layout |
| Where to click | The button or menu, highlighted |
| Forms to fill | The form with the right values entered |
| What success looks like | The confirmation message or finished state |
| What errors look like | The error message, so the reader recognizes it |
| Decision points | Good record vs bad record, side by side |

### Skip

- Steps with nothing on screen (thinking, deciding, phone calls)
- Actions already shown earlier in the same document
- Obvious confirmations (clicking OK, Save, Close) unless the button is hard to find

## Coverage by Document Type

| Document type | Coverage |
|---------------|----------|
| **Step guide** | Every step, no exceptions. One action = one shot. Plus a final result shot at "Done when". |
| **SOP** | Every unfamiliar screen and every decision point; roughly 1 spec per 2-3 UI actions. |
| **Playbook** | 1 per major section that touches a tool. |

Every document ships at least: one overview shot (the main screen), one action shot (what to click), one result shot (what done looks like).

## Redaction

Assume every guide gets forwarded. Before a document leaves the team, the capture person blurs:

- Email addresses, phone numbers, and mailing addresses of real people
- Customer or seller names in records
- Account balances, API keys, anything in a settings screen you would not post publicly

Put a redaction reminder in the document's capture notes when the tool shows live records (CRMs always do).

## Capturing the Shots

For whoever fills the placeholders in:

1. Work through the process for real; capture at each spec as you go. The shots must show real state, not a staged empty account (redact after, not fake before).
2. Use the OS screenshot tool at full resolution; annotate with a single rectangle or circle in one consistent color across the whole document.
3. Crop per the spec. Keep the original uncropped shot until the document is approved, in case a wider view is needed.
4. Drop each image directly under its yellow placeholder box in the Word doc, then delete the placeholder.
