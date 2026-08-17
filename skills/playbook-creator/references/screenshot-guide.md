# Screenshot Guide

This guide explains when and how to add screenshot placeholders. All visuals should be **actual screenshots of software interfaces**—not custom illustrations, diagrams, or graphics. This keeps the workload low while still adding value.

---

## Screenshots Only—No Custom Graphics

| Use | Don't Use |
|-----|-----------|
| Screenshots of software interfaces | Custom illustrations |
| Screenshots of dashboards and data | Hand-drawn diagrams |
| Screenshots of buttons and menus | Infographics |
| Screenshots of forms and settings | Flowcharts (unless from software) |
| Screenshots of results and outputs | Custom icons or graphics |

**Why screenshots only?** Custom graphics take a lot of time to create. Screenshots are quick to capture and show exactly what the reader will see.

---

## Screenshot Placeholder Format

Use this format for all screenshot placeholders:

```markdown
> **SCREENSHOT: [Brief Description]**
> 
> *Capture: [What to show in the screenshot]*
> *Purpose: [Why this screenshot helps]*
```

### Placeholder Best Practices

1. **Format:** Always use `[SCREENSHOT: {description of what to capture}]` or the blockquote format above. Be consistent within one document.
2. **Placement:** Put screenshots AFTER the step they illustrate, not before. The reader does the action first, then sees the confirmation.
3. **Be specific about context:** Include what screen the user should be on, what element to highlight, and what state it should be in.

**Good placeholder:**
```markdown
> **SCREENSHOT: DataSift Upload Wizard - Column Mapping (Step 4)**
>
> *Capture: The column mapping screen with "Tags" column highlighted, showing the drag-and-drop area. The "Property Street" column should already be auto-mapped (green check).*
> *Purpose: Shows which columns need manual mapping vs auto-mapped ones*
```

**Bad placeholder:**
```markdown
> **SCREENSHOT: Upload screen**
>
> *Capture: The screen*
> *Purpose: For reference*
```

4. **Minimum coverage:** At least 1 screenshot per major decision point or unfamiliar UI screen. If a step involves a screen the reader has never seen, add a placeholder.
5. **Do not over-screenshot:** Skip obvious steps like clicking "OK", "Save", or "Close" buttons. If the reader can figure it out without a picture, skip it.

---

## When to Add Screenshots

### Always Add Screenshots For:

| Situation | Example |
|-----------|---------|
| **First time showing a tool** | "Main Dashboard" - Shows the overall layout |
| **Where to click** | "Left Menu" - Shows which menu to open |
| **Buttons to press** | "Export Button" - Shows where the button is |
| **Forms to fill out** | "Settings Form" - Shows what to enter |
| **What success looks like** | "Upload Complete Message" - Shows the result |
| **What errors look like** | "Error Message" - Shows what went wrong |
| **Data and numbers** | "Analytics View" - Shows charts or metrics |
| **Before and after** | "Record Before" vs "Record After" |
| **Decision points** | "Good Record vs Bad Record" - Shows what each looks like |

### Skip Screenshots For:

- Simple text steps that don't involve clicking anything
- Ideas or concepts without a visual part
- Steps that are just thinking or deciding
- Actions you already showed earlier
- Obvious UI actions (clicking OK, Save, Close, or Cancel buttons)

---

## Good vs Bad Descriptions

### Good Descriptions

| Part | Good Example | Why It Works |
|------|-------------|--------------|
| **Brief Description** | "Filter Panel Settings" | Clear and specific |
| **Capture** | "The filter menu with 'Date Range' selected" | Says exactly what to show |
| **Purpose** | "Shows which filters to pick" | Explains why it helps |

### Bad Descriptions

| Part | Bad Example | Problem |
|------|-------------|---------|
| **Brief Description** | "Screenshot" | Too vague |
| **Capture** | "The screen" | Not specific |
| **Purpose** | "For reference" | Doesn't explain why |

---

## Screenshot Types

### Overview Screenshots

Use when showing a new tool for the first time:

```markdown
> **📸 SCREENSHOT: [Tool Name] Main Screen**
> 
> *Capture: The full screen showing the main dashboard*
> *Purpose: Shows the reader the overall layout before going into details*
```

### Action Screenshots

Use when showing what to click:

```markdown
> **📸 SCREENSHOT: [Button/Menu Name]**
> 
> *Capture: The button or menu item, highlighted or circled*
> *Purpose: Shows exactly where to click*
```

### Settings Screenshots

Use when showing forms or configuration:

```markdown
> **📸 SCREENSHOT: [Feature] Settings**
> 
> *Capture: The settings screen with the right values filled in*
> *Purpose: Shows the reader what to enter*
```

### Result Screenshots

Use when showing what happens after a step:

```markdown
> **📸 SCREENSHOT: [Process] Complete**
> 
> *Capture: The success message or result screen*
> *Purpose: Shows what the reader should see when done*
```

### Before/After Screenshots

Use when showing a change:

```markdown
> **📸 SCREENSHOT: Before vs After**
> 
> *Capture: Two screenshots showing the change*
> *Purpose: Shows what changed and why it matters*
```

---

## Where to Put Screenshots

1. **After the step, not before**: Put the screenshot right after the action it shows
2. **One screenshot per big action**: Don't over-do it on simple steps
3. **Show enough context**: Include enough of the screen so readers know where they are

### Spacing

Add a blank line before and after each screenshot:

```markdown
Click the **Export** button in the top-right corner.

> **📸 SCREENSHOT: Export Button**
> 
> *Capture: Top-right toolbar with Export button highlighted*
> *Purpose: Shows where the button is*

After clicking Export, you'll see the download options.
```

---

## How Many Screenshots

| Document Type | How Often |
|--------------|-----------|
| **SOP (Technical)** | 1 screenshot every 2-3 steps |
| **SOP (Simple)** | 1 screenshot every 4-5 steps |
| **Playbook (Concept-heavy)** | 1 screenshot per major section |
| **Playbook (Process-heavy)** | 1 screenshot every 3-4 steps |

### Minimum Screenshots

Every document should have at least:
- 1 overview screenshot (shows the main screen)
- 1 action screenshot (shows what to click)
- 1 result screenshot (shows what success looks like)
