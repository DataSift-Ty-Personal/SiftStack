---
name: playbook-creator
description: Create professional playbooks, SOPs, and Scribe-style step guides with process maps and agent-executable SOP files. Accepts raw transcripts, meeting recordings, screen-share walkthroughs, or written descriptions and turns them into polished documentation with Mermaid flowcharts, decision trees, and screenshot capture specs. SOPs ship as a pair: a human Word doc plus a machine-readable .sop.md twin (Agent SOPs standard, RFC 2119 constraints) an AI agent can execute directly. Use when someone asks for a playbook, SOP, standard operating procedure, process documentation, training manual, workflow guide, click-path guide, process map or flowchart, or says "turn this into a playbook", "create an SOP from this call", "document this process", "make a guide like Scribe", or "make this runnable by an agent". Also use when a transcript describes a repeatable process and the ask is just to make it useful.
---

# Playbook & SOP Creator

This skill creates professional documentation in three formats:

1. **SOP** - step-by-step operational procedure for a specific, repeatable task. Ships as a PAIR: a human Word doc plus an agent-executable `.sop.md` twin.
2. **Playbook** - strategic framework covering a system of processes, with mental models and decision logic.
3. **Step Guide** - a Scribe-style click path: one screen action per step, one screenshot per step, nothing else.

All formats include process maps built with Mermaid where flow needs visualizing, screenshot capture specs for software interfaces, and are written at a 5th-grade reading level so anyone on a team can follow them.

The skill works from multiple input types: raw transcripts (meetings, training calls, screen shares), written descriptions, existing documentation, or just a topic the user wants documented. When working from transcripts, it extracts the actual workflow being demonstrated - the clicks, the decisions, the order of operations - and structures it into clean documentation.

## Input Detection

Understand what the user gave you and adapt:

| Input Type | What to Do |
|-----------|------------|
| **Raw transcript** (meeting notes, Fireflies, call recording) | Extract the workflow being taught. Identify the teacher vs. learner. Pull out the actual steps, decision points, tools used, and tips mentioned. Ignore small talk and tangents. |
| **Topic only** ("create a playbook for pulling foreclosures") | Research the topic using available tools and your knowledge. Ask clarifying questions about their specific tools and workflow. |
| **Existing doc** (rough notes, bullet points, old SOP) | Restructure into the proper template. Improve clarity. Add process maps and visual aids. |
| **Screen share / walkthrough description** | Treat like a transcript - extract the sequential actions, what was clicked, what was checked, and what decisions were made. This input usually wants a Step Guide. |
| **Video recording** (screen recording with narration) | The richest input: the narration carries the steps and judgment, the frames ARE the screenshots. See "Video Input" below. |

### Video Input

A narrated screen recording is the ideal source. Process it in two tracks:

1. **Narration to transcript.** Run the bundled script (needs ffmpeg on PATH and `OPENROUTER_API_KEY` in the environment or `.env`; about $0.002 per audio minute):

```bash
python <skill-path>/scripts/transcribe_video.py walkthrough.mp4 --frames-dir images/
```

It writes `walkthrough.transcript.md` with three sections: `## TRANSCRIPT` (timestamped narration), `## ACTIONS` (every UI action with the exact label spoken), `## JUDGMENT` (every decision rule and warning stated). The JUDGMENT section is the gold: those lines become the SOP's Rules, Pro Tips, and the agent twin's MUST NOT constraints.

2. **Frames to screenshots.** `--frames-dir` also extracts a video frame at each action timestamp (with automatic clock-drift correction) - those frames ARE the document's screenshots. Pass `--crop w:h:x:y` to cut browser chrome and taskbar (1080p full-screen Chrome: `1920:910:0:125`). Then VERIFY every frame visually against its step: drift correction is approximate, a frame can catch a page mid-load, and near-duplicates should be dropped. Re-pull a bad frame with a nudged timestamp. Embed the keepers as markdown image lines.

**No key? Still works.** Paste the recorder's own free transcript (Loom, Zoom, and Fireflies all auto-transcribe) and extract frames manually with ffmpeg at the transcript's timestamps. Flag any frame showing real customer data for blurring before the document leaves the team.

When working from a transcript, pay close attention to:
- **The order things happen** - this becomes your step sequence
- **Decision points** ("if it's a mobile home, mark it dead" / "if there's no address, skip it") - these become decision gates, flowchart branches, and constraints in the agent twin
- **Tools and screens mentioned** - these become screenshot specs
- **Tips and warnings** ("anytime it says 2800, I'm skeptical") - these become best practices, callout boxes, and MUST NOT constraints with reasons
- **Repeated patterns** - if the trainer does the same thing multiple times with different records, that's one step with examples, not multiple steps

### Transcript Quality Assessment

Before starting, assess the input quality. This tells you how much cleanup work is ahead.

| Quality | Signs | What to Expect |
|---------|-------|----------------|
| **GOOD** | Clear steps, specific tools/screens named, defined outcomes, under 60 min recording | Straightforward extraction. Minimal clarification needed. |
| **OKAY** | General guidance, some specific steps, may jump around a bit | Workable but you may need to ask 1-2 clarifying questions about order or tools. |
| **POOR** | Rambling, no clear steps, multiple topics mixed together, over 90 min without structure | Needs focus. Ask the user to pick the ONE process they want documented first. |

**When input is POOR:** Ask the user: "This covers several topics. Which ONE process should I document first?" Then extract just that process from the transcript. Do not try to document everything in one pass.

**Never refuse to create.** Always produce something, even from poor input. Flag sections that need user review with a note like: `> **Needs Review:** This step was unclear in the source. Please confirm the exact action.`

**Never fabricate a step.** If the source does not say what happens between two actions, mark the gap for review. A guessed click in an SOP gets executed by someone who trusts it.

## Document Type Selection

| Signal | Type |
|--------|------|
| "SOP", "procedure", "process", "workflow", "steps"; ONE task with decisions, checks, or work outside the screen | **SOP** |
| "playbook", "handbook", "framework", "strategy"; a SYSTEM of processes, multiple roles, branching paths | **Playbook** |
| "guide", "how-to", "show them where to click", "like Scribe"; a pure in-app click path with at most one simple fork | **Step Guide** |

Decision criteria when keywords alone are not enough:

| Criteria | Step Guide | SOP | Playbook |
|----------|-----------|-----|----------|
| **Scope** | One click path in one app | Single process | Multi-process system |
| **Flow** | Linear, at most one fork | Linear with decision gates | Branching, multiple paths |
| **Judgment** | None - just follow the screen | Some - thresholds, checks | Constant - strategy and tradeoffs |
| **Roles** | One person at a keyboard | One role performs it | Multiple roles or handoffs |
| **Example** | "How to upload a CSV to DataSift" | "How to qualify a foreclosure record" | "Foreclosure acquisition playbook" |

**The decision rule:** If the reader never leaves the app and never has to think, make a Step Guide. If the input describes ONE procedure with real decisions, make an SOP. If it describes a SYSTEM, make a Playbook.

**Defaults by input type:**
- Screen-share walkthrough of one tool → Step Guide
- Transcript of a single task with judgment calls → SOP
- Strategic or multi-process training content → Playbook
- Topic request covering an entire business function → Playbook

A Playbook may EMBED step-guide-formatted sections and reference SOPs. An SOP may link out to a Step Guide for a mechanical sub-sequence. Compose; do not cram.

### Page Length Guidance

| Document Type | Target Length | Hard Limits |
|--------------|--------------|-------------|
| **Step Guide** | 5-25 steps | Under 5, fold into another doc. Over 25, split into parts. |
| **SOP** | 5-7 pages | 3-10 pages. If over 10, split into multiple SOPs. |
| **Playbook** | 25-35 pages | 15-50 pages. If over 50, break into chapters or volumes. |

## Creation Workflow

1. Detect input type (transcript, topic, existing doc, walkthrough)
2. If transcript: extract workflow, decisions, tools, tips
3. Pick document type (Step Guide, SOP, or Playbook)
4. Create outline using the right template
5. Build process maps (Mermaid flowcharts for the main workflow and key decision points; step guides skip these)
6. Write content with screenshot capture specs
7. Add decision gates, best practices, and quality checks
8. Include at least one worked example walking through a real scenario (SOP and Playbook)
9. **For SOPs: write the agent twin (`.sop.md`) and validate it** (see The Agent Twin below)
10. Deliver: Word doc via the build script, plus the `.md` source and any `.sop.md` twin

## The Agent Twin (.sop.md)

Every SOP whose steps an agent could do or help with - data pulls, filtering, CRM updates, research, report building - ships with a second file: an agent-executable SOP in the open Agent SOPs format (the strands-agents standard). Purely physical processes (walking a property, courthouse photo runs) skip the twin.

The format's four structural invariants:
1. Filename ends `.sop.md`, kebab-case
2. `## Overview` - 2-4 self-contained sentences (doubles as the description wherever the SOP is listed)
3. `## Parameters` - snake_case inputs with a mandatory `**Constraints for parameter acquisition:**` block
4. `## Steps` - numbered `### 1. Step Name` headings, each with a `**Constraints:**` block of RFC 2119 lines (You MUST / You SHOULD / You MAY)

Three rules that make twins actually work:
- **Every negative constraint carries a reason.** "You MUST NOT [action] because [reason]." A bare prohibition gets ignored.
- **Every step that produces something names its artifact path.** That is the resumability story: a broken run is picked up by reading the artifacts.
- **Validate after every edit:** `python <skill-path>/scripts/validate_sop.py <file>.sop.md` - fix all errors, read all warnings.

The human SOP's Inputs table and the twin's Parameters use the SAME names, so the two documents never drift apart. Full spec, skeleton, and constraint guidance: [references/agent-sop-format.md](references/agent-sop-format.md).

## Step Guide Format (Scribe Style)

The step guide mirrors how Scribe auto-generates guides. Exact shape:

```markdown
# [Title under 60 chars, names the app and the task]

[N] steps · Created [Month D, YYYY] · Source: [app name]

---

## Step 01: Click the [label] button

> **SCREENSHOT: [label]**
>
> *Capture: [screen, in what state]*
> *Highlight: [the one element]*
> *Crop: [zoom guidance]*
```

Non-negotiables:
- **The heading IS the instruction**: one sentence, starts with an action verb (Click / Enter / Select / Toggle / Press / Navigate / Set), names the target by its exact on-screen label in bold
- **Step numbers zero-padded** (`Step 01`, not `Step 1`)
- **One action per step, one screenshot per step** - "Click Save, then Confirm" is two steps
- **Typing coalesces**: one step per field, never per keystroke
- **Close with "Done when:"** plus a result screenshot spec

Full template, phrasing table, and branching rules: [references/step-guide-template.md](references/step-guide-template.md).

## Process Maps & Visual Aids

Process maps give the reader a bird's-eye view of the workflow before the details, and make decision points crystal clear. Every SOP and Playbook includes at least one. Step guides never need one (they are linear by definition; if you feel the need for a flowchart, it should have been an SOP).

See [references/process-mapping-guide.md](references/process-mapping-guide.md) for the complete guide on building Mermaid flowcharts, decision trees, and swim lane diagrams.

### When to Use Each Type

| Visual Type | When to Use | Example |
|------------|-------------|---------|
| **Linear flowchart** | Simple A-to-B-to-C processes with few branches | "How to add a record to Sift" |
| **Decision tree** | Processes with lots of IF/THEN branching | "How to qualify a foreclosure record" |
| **Swim lane diagram** | Processes that span multiple people or tools | "Lead flow from data pull to closing" |
| **Status progression** | Showing how a record moves through stages | "Record lifecycle from raw data to deal" |

### Chart Size Limits (Critical)

**No single chart should have more than 7 nodes.** Large charts render too small to read in the Word document. This is the most common quality issue.

**How to handle large processes:**
1. Create a **high-level overview chart** (4-6 nodes) showing the major phases
2. Create **separate detail charts** (5-7 nodes each) for each phase or complex decision
3. Put the overview chart after the Purpose section; put detail charts at the start of each phase

### Minimum Visual Aids Per Document

| Document Type | Minimum Visuals |
|--------------|----------------|
| **Step Guide** | Screenshot spec on every step. No flowcharts. |
| **SOP (under 10 steps)** | 1 overview flowchart + decision trees at complex steps + screenshot specs |
| **SOP (10+ steps)** | 1 overview flowchart + detail charts per phase + decision trees + screenshot specs |
| **Playbook** | 1 overview flowchart + detail charts per section + decision trees + screenshot specs |

## Reading Level: 5th Grade

All content must be written at a **5th-grade reading level**. A new hire, a VA overseas, or a busy operator scanning between calls should all be able to follow it.

**Rules:**
- Short sentences (under 20 words)
- Common, everyday words
- One idea per sentence
- Active voice ("Click the button" not "The button should be clicked")

**Word Swaps:**

| Replace This | With This |
|--------------|-----------|
| utilize | use |
| implement | set up, start |
| leverage | use |
| optimize | improve |
| facilitate | help |
| comprehensive | complete, full |
| subsequently | then, next |
| methodology | method, way |
| prioritize | focus on |
| maximize | get the most from |

The agent twin is the one exception: its constraint lines use the RFC 2119 register (You MUST / You SHOULD) because an agent is the reader.

See [references/voice-guide.md](references/voice-guide.md) for the complete guide.

## Voice & Writing Style

Write like a helpful team lead who has done this process a hundred times and is showing someone new exactly how it works. Every sentence has a job. No fluff, no filler, no corporate speak.

| Principle | What It Means |
|-----------|---------------|
| Be direct | Say things plainly. Don't hedge. |
| Be practical | Focus on what to do and why it matters. |
| Be specific | Use real numbers, times, tool names, and exact UI labels. |
| Be natural | Write like you're talking to a teammate. |

**What to Avoid:**

| Avoid | Why |
|-------|-----|
| Meta-language ("the metaphor is...", "this framework...") | Just say the concept directly |
| Signature phrases ("Here's the thing...") | They get repetitive across docs |
| Big words when small words work | Keep it at 5th-grade level |
| Filler transitions ("Furthermore...") | Use simple words or just start the next sentence |
| Repeating what you just said | Trust the reader. Move on. |

## Screenshot Capture Specs

Use **actual screenshots of software interfaces only** - no custom illustrations. Documents contain capture SPECS in Scribe's annotation style, so whoever takes the shots knows exactly what to capture, what to highlight, and how to crop:

```markdown
> **SCREENSHOT: [Short label]**
>
> *Capture: [What screen, in what state]*
> *Highlight: [The ONE element to box or circle]*
> *Crop: [How tight to zoom, keeping enough context to orient]*
```

Rules: exactly one highlighted element per shot; capture before the click for actions, after for results; crop centered on the element with about a third of the frame as context; redact real customer data before the doc leaves the team. Full guide: [references/screenshot-guide.md](references/screenshot-guide.md).

## Playbook Format

Playbooks teach strategic frameworks and concepts. They answer "how should I think about this?" See [references/playbook-template.md](references/playbook-template.md) for the full template.

**Structure:**
- Title with motto
- Table of Contents
- Overview with process map (Mermaid flowchart of the full workflow)
- Core Concept section with central metaphor (introduced naturally, not announced)
- Framework/Process section with decision trees where paths split
- Worked Example (walk through a real scenario start to finish)
- Best Practices
- Implementation Checklist
- Quick Reference

## SOP Format

SOPs give step-by-step instructions for a specific task. They answer "how do I do this?" See [references/sop-template.md](references/sop-template.md) for the full template, including the human-to-twin mapping table.

**Structure:**
- Title + metadata line (step count, created date, owner)
- Purpose & Overview with an explicit **"Done when"** end state
- Process Map (Mermaid flowchart of all steps)
- What You Need: tools, an **Inputs table** (names shared with the agent twin's Parameters), setup
- Step-by-Step Process: each step has a Goal, action-verb Actions naming exact UI labels, Rules (must/never with reasons), a Check, and a **"Record it"** line naming where the step's output lands
- Worked Example (one record walked through every step)
- Decision Guide, Quality Check, Troubleshooting, Quick Reference

## Worked Examples

Every SOP and Playbook includes at least one **worked example** - a complete walkthrough of one real scenario from start to finish. When working from a transcript, the transcript itself often IS the worked example: the trainer was walking through a real record. Extract that walkthrough, including the decisions they made and why.

```markdown
### Worked Example: [Scenario Name]

**Starting point:** [What you're starting with]

**Step 1 applied:** [What you do and what you see]
**Decision:** [What you decided and why]
**Step 2 applied:** [What you do next]
...continue through all steps...

**End result:** [What the finished product looks like]
```

See [references/foreclosure-example.md](references/foreclosure-example.md) for a complete worked example showing how a training call transcript was turned into an SOP.

## Formatting Rules

- **Bold** for key terms, UI elements (exact on-screen labels), tool names
- `code formatting` for exact text to type, file names, or field values
- Tables for comparisons and structured data
- Blockquotes for key insights, pro tips, and screenshot specs
- Numbered lists for sequential steps
- Mermaid code blocks for process maps and decision trees
- Horizontal rules (`---`) between major sections

**Decision Points:**
```markdown
**Decision Gate:**
- IF [condition] → [action/path]
- IF [other condition] → [other action/path]
```
For complex decision points (3+ branches), also include a Mermaid decision tree diagram above the text version.

**Pro Tips (from transcript insights):**
```markdown
> **Pro Tip:** [Practical insight from experience]
```

## Output Requirements

The primary human deliverable is a **Word document (.docx)** with all Mermaid flowcharts rendered as embedded images. Word format lets the team open it in Word or Google Docs, drop in actual screenshots where the placeholders are, and edit as needed.

Deliverables per document type:

| Type | Files delivered |
|------|-----------------|
| Step Guide | `.docx` + `.md` source |
| SOP | `.docx` + `.md` source + `<name>.sop.md` agent twin (when agent-runnable) |
| Playbook | `.docx` + `.md` source |

### How to Build the Word Document

1. **Write the content as Markdown first**, with all Mermaid code blocks, tables, screenshot specs, and formatting as described in this skill.
2. **Run the build script:**

```bash
node <skill-path>/scripts/build_docx.js input.md output.docx --title "Document Title"
```

The build script parses the Markdown, renders every Mermaid block to PNG via the Mermaid CLI (mmdc), and embeds everything in a styled Word document (styled headers, colored tables, green Pro Tip callouts, yellow screenshot-spec callouts, page numbers).

**Real screenshots embed directly.** A standalone markdown image line (`![Step 01](step01.png)` with a local path, or a base64 data URI like a Scribe-style capture-tool export) is embedded in the Word doc at content width with the alt text as its caption. So when screenshots already exist, put the image line where the capture spec would go and the doc ships finished; use the yellow spec callouts only for shots nobody has taken yet. When the process lives in a browser and Playwright (or similar) is available, capture the screenshots yourself at each step, draw a single highlight box on the click target, save them next to the markdown, and reference them, delivering a finished doc with no capture work left for the user.

**Dependencies:** `docx` and `@mermaid-js/mermaid-cli` npm packages (`npm install docx @mermaid-js/mermaid-cli` if missing).

**Without mmdc:** flowcharts appear as text Mermaid blocks instead of images; everything else renders normally. Cosmetic only - the user can paste any block into mermaid.live and drop the image in manually.

3. **For SOP twins, validate before delivering:**

```bash
python <skill-path>/scripts/validate_sop.py <name>.sop.md
```

Fix every error. The twin is not done until the validator passes.

### Delivery

Give the user the Word doc as the primary deliverable, keep the `.md` as the editable source, and hand over the `.sop.md` twin with one line on how to use it: paste it to an agent (or install it as a skill/MCP prompt) and the agent will ask for the parameters and run the steps.

## Investment Blueprint Definitions

When generating acquisition playbooks, these are the four blueprint types. Definitions match `src/playbook_generator.py`.

| Blueprint | Timeline | Exit Strategy | Key Detail |
|-----------|----------|---------------|------------|
| **Wholesale** | 7-14 day close | Assignment or double-close | No rehab, no ownership. Earnest money only. |
| **Flip** | 3-6 month cycle | Full rehab + retail sale | Highest profit and highest risk. $30K-$100K+ capital. |
| **Buy-and-Hold** | 30-60 day acquisition | Long-term rental | Cash flow + appreciation. $20K-$50K capital. |
| **Hybrid** | Mixed | Wholesale most, cherry-pick flips | Volume from wholesale, margin from flips. |

## Reference Files

- **Voice Guide**: [references/voice-guide.md](references/voice-guide.md) - Writing style and reading level rules
- **Process Mapping Guide**: [references/process-mapping-guide.md](references/process-mapping-guide.md) - Mermaid flowcharts, decision trees, swim lanes
- **Playbook Template**: [references/playbook-template.md](references/playbook-template.md) - Full template for playbook documents
- **SOP Template**: [references/sop-template.md](references/sop-template.md) - Full template for SOPs, with the agent-twin mapping
- **Agent SOP Format**: [references/agent-sop-format.md](references/agent-sop-format.md) - The `.sop.md` spec: parameters, RFC 2119 constraints, artifacts, validation
- **Step Guide Template**: [references/step-guide-template.md](references/step-guide-template.md) - The Scribe-style click-path format
- **Screenshot Guide**: [references/screenshot-guide.md](references/screenshot-guide.md) - Capture + highlight + crop specs
- **Foreclosure Example**: [references/foreclosure-example.md](references/foreclosure-example.md) - Transcript turned into an SOP, end to end
- **DOCX Build Script**: [scripts/build_docx.js](scripts/build_docx.js) - Markdown + Mermaid to formatted Word
- **SOP Validator**: [scripts/validate_sop.py](scripts/validate_sop.py) - Structural validation for `.sop.md` files
