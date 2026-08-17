---
name: playbook-creator
description: Create professional playbooks and SOPs with process maps, visual aids, and structured workflows. Accepts raw transcripts, meeting recordings, or written descriptions as input and transforms them into polished documentation with Mermaid flowcharts, decision trees, and screenshot placeholders. Use when user requests a playbook, SOP, standard operating procedure, process documentation, training manual, workflow guide, or says things like "turn this into a playbook", "create an SOP from this call", "document this process", "make a training doc", or "write up how to do X". Also triggers for "process map", "flowchart", "workflow diagram", or any request to visualize a process. Even if the user just gives you a transcript and says "make this into something useful" — if it describes a repeatable process, use this skill.
---

# Playbook & SOP Creator

This skill creates professional documentation in two formats: **Playbooks** (strategic frameworks with metaphors and mental models) and **SOPs** (step-by-step operational procedures). Both formats include process maps built with Mermaid, screenshot placeholders for software interfaces, and are written at a 5th-grade reading level so anyone on a team can follow them.

The skill can work from multiple input types: raw transcripts (meetings, training calls, screen shares), written descriptions, existing documentation, or just a topic the user wants documented. When working from transcripts, it extracts the actual workflow being demonstrated — the clicks, the decisions, the order of operations — and structures it into clean documentation.

## Input Detection

Understand what the user gave you and adapt:

| Input Type | What to Do |
|-----------|------------|
| **Raw transcript** (meeting notes, Fireflies, call recording) | Extract the workflow being taught. Identify the teacher vs. learner. Pull out the actual steps, decision points, tools used, and tips mentioned. Ignore small talk and tangents. |
| **Topic only** ("create a playbook for pulling foreclosures") | Research the topic using available tools and your knowledge. Ask clarifying questions about their specific tools and workflow. |
| **Existing doc** (rough notes, bullet points, old SOP) | Restructure into the proper template. Improve clarity. Add process maps and visual aids. |
| **Screen share / walkthrough description** | Treat like a transcript — extract the sequential actions, what was clicked, what was checked, and what decisions were made. |

When working from a transcript, pay close attention to:
- **The order things happen** — this becomes your step sequence
- **Decision points** ("if it's a mobile home, mark it dead" / "if there's no address, skip it") — these become decision gates and flowchart branches
- **Tools and screens mentioned** — these become screenshot placeholders
- **Tips and warnings** ("anytime it says 2800, I'm skeptical") — these become best practices and callout boxes
- **Repeated patterns** — if the trainer does the same thing multiple times with different records, that's one step with examples, not multiple steps

### Transcript Quality Assessment

Before starting, assess the input quality. This tells you how much cleanup work is ahead.

| Quality | Signs | What to Expect |
|---------|-------|----------------|
| **GOOD** | Clear steps, specific tools/screens named, defined outcomes, under 60 min recording | Straightforward extraction. Minimal clarification needed. |
| **OKAY** | General guidance, some specific steps, may jump around a bit | Workable but you may need to ask 1-2 clarifying questions about order or tools. |
| **POOR** | Rambling, no clear steps, multiple topics mixed together, over 90 min without structure | Needs focus. Ask the user to pick the ONE process they want documented first. |

**When input is POOR:** Ask the user: "This covers several topics. Which ONE process should I document first?" Then extract just that process from the transcript. Do not try to document everything in one pass.

**Never refuse to create.** Always produce something, even from poor input. Flag sections that need user review with a note like: `> **Needs Review:** This step was unclear in the source. Please confirm the exact action.`

## Document Type Selection

Pick the document type based on what the user needs:

| Request Contains | Document Type | Use When |
|-----------------|---------------|----------|
| "playbook", "handbook", "framework", "strategy", "guide" | Playbook | Teaching concepts, frameworks, or strategic approaches. Has a central metaphor. |
| "SOP", "procedure", "process", "workflow", "how-to", "steps" | SOP | Step-by-step instructions for a specific, repeatable task. No metaphor needed. |

**If unclear**: Default to SOP for transcripts that show someone doing a specific task. Default to Playbook when the content covers strategy or multiple related processes.

### Playbook vs SOP Decision Criteria

Use this table when the request keywords alone are not enough to decide:

| Criteria | SOP | Playbook |
|----------|-----|----------|
| **Scope** | Single process | Multi-process system |
| **Flow** | Linear steps, few branches | Branching decisions, multiple paths |
| **Roles** | One role performs it | Multiple roles or handoffs |
| **Duration** | Under 30 minutes to complete | Over 30 minutes, or spans multiple days |
| **Examples** | "How to upload a CSV to DataSift", "How to run a skip trace" | "Foreclosure acquisition playbook", "New market entry playbook" |

**The decision rule:** If the input describes ONE procedure, make an SOP. If it describes a SYSTEM with multiple procedures, make a Playbook.

**Defaults by input type:**
- Transcript of a single task (screen share, training walkthrough) → SOP
- Strategic or multi-step training content (onboarding, campaign setup) → Playbook
- Written notes describing one repeatable task → SOP
- Topic request covering an entire business function → Playbook

### Page Length Guidance

| Document Type | Target Length | Hard Limits |
|--------------|--------------|-------------|
| **SOP** | 5-7 pages | 3-10 pages. If over 10, split into multiple SOPs. |
| **Playbook** | 25-35 pages | 15-50 pages. If over 50, break into chapters or volumes. |

**Section limits:**
- Each SOP section: 1-2 pages max
- Each Playbook chapter: 5-10 pages max

These are guidelines for the final Word document, not the Markdown source. Mermaid diagrams and screenshot placeholders add page length when rendered.

## Creation Workflow

1. Detect input type (transcript, topic, existing doc)
2. If transcript: extract workflow, decisions, tools, tips
3. Pick document type (Playbook or SOP)
4. Create outline using the right template
5. Build process maps (Mermaid flowcharts for the main workflow and key decision points)
6. Write content with visual aids and screenshot placeholders
7. Add decision gates, best practices, and quality checks
8. Include at least one worked example walking through a real scenario
9. Deliver as Markdown file

## Process Maps & Visual Aids

Process maps are one of the most valuable parts of a playbook or SOP. They give the reader a bird's-eye view of the entire workflow before diving into details, and they make decision points crystal clear. Every playbook and SOP should include at least one process map.

See [references/process-mapping-guide.md](references/process-mapping-guide.md) for the complete guide on building Mermaid flowcharts, decision trees, and swim lane diagrams.

### When to Use Each Type

| Visual Type | When to Use | Example |
|------------|-------------|---------|
| **Linear flowchart** | Simple A-to-B-to-C processes with few branches | "How to add a record to Sift" |
| **Decision tree** | Processes with lots of IF/THEN branching | "How to qualify a foreclosure record" |
| **Swim lane diagram** | Processes that span multiple people or tools | "Lead flow from data pull to closing" |
| **Status progression** | Showing how a record moves through stages | "Record lifecycle from raw data to deal" |

### Chart Size Limits (Critical)

**No single chart should have more than 7 nodes.** Large charts with 10+ nodes render too small to read in the Word document. This is the most common quality issue.

**How to handle large processes:**
1. Create a **high-level overview chart** (4-6 nodes) showing the major phases
2. Create **separate detail charts** (5-7 nodes each) for each phase or complex decision
3. Put the overview chart after the Purpose section; put detail charts at the start of each step

**Example — Splitting a 12-step process:**

BAD (too large — renders unreadable in Word):
```
1 chart with 12 nodes: Start → Step 1 → Step 2 → Decision → Step 3a → Step 3b → Step 4 → Decision → Step 5a → Step 5b → Step 6 → End
```

GOOD (3 readable charts):
```
Overview chart (4 nodes):  Setup Phase → Research Phase → Decision Phase → Execution Phase
Detail chart 1 (5 nodes): Step 1 → Step 2 → Decision? → Step 3a / Step 3b
Detail chart 2 (5 nodes): Step 4 → Decision? → Step 5a / Step 5b → Step 6
```
Place the overview chart after Purpose. Place each detail chart at the start of its corresponding phase section.

See [references/process-mapping-guide.md](references/process-mapping-guide.md) for the complete guide including the node count table and segmentation rules.

### Minimum Visual Aids Per Document

| Document Type | Minimum Visuals |
|--------------|----------------|
| **SOP (under 10 steps)** | 1 overview flowchart + decision trees at complex steps + screenshot placeholders |
| **SOP (10+ steps)** | 1 overview flowchart + detail charts per phase + decision trees + screenshot placeholders |
| **Playbook** | 1 overview flowchart + detail charts per section + decision trees + screenshot placeholders |

### Placement

Put the overview flowchart right after the overview/introduction section — before the detailed steps. This gives the reader a map before the journey. Put detail charts and decision trees at the start of the step or phase they cover.

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

See [references/voice-guide.md](references/voice-guide.md) for the complete guide.

## Voice & Writing Style

Write like a helpful team lead who has done this process a hundred times and is showing someone new exactly how it works. Every sentence has a job. No fluff, no filler, no corporate speak.

**Core Principles:**

| Principle | What It Means |
|-----------|---------------|
| Be direct | Say things plainly. Don't hedge. |
| Be practical | Focus on what to do and why it matters. |
| Be specific | Use real numbers, times, tool names, and examples. |
| Be natural | Write like you're talking to a teammate. |

**What to Avoid:**

| Avoid | Why |
|-------|-----|
| Meta-language ("the metaphor is...", "this framework...") | Just say the concept directly |
| Signature phrases ("Here's the thing...") | They get repetitive across docs |
| Big words when small words work | Keep it at 5th-grade level |
| Filler transitions ("Furthermore...") | Use simple words or just start the next sentence |
| Repeating what you just said | Trust the reader. Move on. |

## Screenshot Placeholders

Use **actual screenshots of software interfaces only** — not custom illustrations or generic graphics. Screenshots show the reader exactly what they'll see on screen.

**Format:**

```markdown
> **SCREENSHOT: [Brief Description]**
>
> *Capture: [What to show in the screenshot]*
> *Purpose: [Why this screenshot helps]*
```

**When to Add:**
- First time showing a tool or interface
- Showing what to click or where to navigate
- Showing expected results or success states
- Showing what an error or problem looks like
- Showing decision points (what does a "good" record vs. "bad" record look like?)

See [references/screenshot-guide.md](references/screenshot-guide.md) for the complete guide.

## Playbook Format

Playbooks teach strategic frameworks and concepts. They answer "how should I think about this?" See [references/playbook-template.md](references/playbook-template.md) for the full template.

**Structure:**
- Title with motto
- Table of Contents
- Overview with process map (Mermaid flowchart of the full workflow)
- Core Concept section with central metaphor
- Framework/Process section with decision trees where paths split
- Worked Example (walk through a real scenario start to finish)
- Best Practices
- Implementation Checklist
- Quick Reference

**Key Elements:**
- **Process Map**: A Mermaid flowchart showing the full workflow at a glance — placed right after the overview
- **Metaphor**: A central metaphor that makes the concept stick — introduced naturally, not announced
- **Decision Trees**: Mermaid diagrams at every major branching point
- **Worked Example**: A full walkthrough with a real scenario so the reader can see the entire process applied to one case
- **Tables**: For comparisons and structured data
- **Callout Boxes**: For key insights (use blockquotes)

## SOP Format

SOPs give step-by-step instructions for a specific task. They answer "how do I do this?" See [references/sop-template.md](references/sop-template.md) for the full template.

**Structure:**
- Title
- Purpose & Overview
- Process Map (Mermaid flowchart of all steps)
- Prerequisites & Setup
- Step-by-Step Process with decision gates
- Worked Example (one record walked through every step)
- Quality Check
- Troubleshooting
- Quick Reference

**Key Elements:**
- **Process Map**: Mermaid flowchart right after the overview — shows all steps and decision points before diving in
- **Objectives**: Each step says what it does
- **Actions**: Specific, numbered instructions
- **Decision Gates**: IF/THEN logic for branching paths, with Mermaid decision trees for complex branches
- **Verification**: How to check each step worked
- **Worked Example**: Walk one real record through every step so the reader sees what "done right" looks like

## Worked Examples

Every playbook and SOP should include at least one **worked example** — a complete walkthrough of one real scenario from start to finish. This is often the most valuable part of the document because it shows how all the pieces fit together.

When working from a transcript, the transcript itself often IS the worked example. The trainer was walking through a real record. Extract that walkthrough and present it as the example, including the decisions they made and why.

**Format for worked examples:**
```markdown
### Worked Example: [Scenario Name]

**Starting point:** [What you're starting with]

**Step 1 applied:** [What you do and what you see]
**Decision:** [What you decided and why]
**Step 2 applied:** [What you do next]
...continue through all steps...

**End result:** [What the finished product looks like]
```

See [references/foreclosure-example.md](references/foreclosure-example.md) for a complete worked example showing how a training call transcript was turned into an SOP for pulling foreclosure data.

## Formatting Rules

- Use **bold** for key terms, UI elements, tool names, and emphasis
- Use `code formatting` for exact text to type, file names, or field values
- Use tables for comparisons and structured data
- Use blockquotes for key insights and pro tips
- Use numbered lists for sequential steps
- Use Mermaid code blocks for process maps and decision trees
- Use horizontal rules (`---`) between major sections

**Decision Points:**
```markdown
**Decision Gate:**
- IF [condition] → [action/path]
- IF [other condition] → [other action/path]
```

For complex decision points (3+ branches), also include a Mermaid decision tree diagram above the text version to make it visual.

**Pro Tips (from transcript insights):**
```markdown
> **Pro Tip:** [Practical insight from experience]
```

## Content Focus

**Include:**
- Process maps (Mermaid flowcharts, decision trees)
- Core metaphor or framework (introduced naturally)
- Clear process steps with objectives
- Decision criteria and gates with visual decision trees
- Screenshot placeholders (UI screenshots only)
- At least one worked example walking through a real scenario
- Pro tips extracted from experience or transcript insights
- Best practices with "what good looks like" and "what to avoid"
- Implementation checklists
- Specific numbers, thresholds, and tool names

**Exclude:**
- Extended storytelling or personal narratives
- Filler content or corporate language
- Vague advice ("be thorough", "do your best")
- Meta-language about the document structure
- Big words when small words work
- Custom graphics or illustrations (use Mermaid diagrams and screenshot placeholders instead)

## Output Requirements

The final deliverable is a **Word document (.docx)** with all Mermaid flowcharts rendered as embedded images. Word format lets the team open it in Word or Google Docs, drop in actual screenshots where the placeholders are, and edit as needed. It's also easy to upload to Google Drive and share.

### How to Build the Word Document

1. **Write the content as Markdown first.** Use all the Mermaid code blocks, tables, screenshot placeholders, and formatting as described in this skill. Save it as a `.md` file.

2. **Run the build script** to convert the Markdown into a polished Word doc with rendered diagrams:

```bash
node <skill-path>/scripts/build_docx.js input.md output.docx --title "Document Title"
```

The build script:
- Parses the Markdown into structured blocks
- Renders every Mermaid code block into a PNG image using the Mermaid CLI (mmdc)
- Embeds the images inline in a formatted Word document using docx-js
- Applies professional styling: styled headers, colored tables with dark header rows, green callout boxes for Pro Tips, yellow callout boxes for Screenshot placeholders, page numbers, and headers

**Dependencies:** Both `docx` and `@mermaid-js/mermaid-cli` npm packages must be installed. Install them with `npm install docx @mermaid-js/mermaid-cli` if not already available.

### What Happens Without mmdc (Mermaid CLI)

The `mmdc` command (from `@mermaid-js/mermaid-cli`) renders Mermaid flowcharts as PNG images in the Word document. If mmdc is not installed:

- **What changes:** Flowcharts appear as text-based Mermaid code blocks instead of visual diagrams.
- **Impact:** The user sees the raw chart code, not a picture. All other content (steps, tables, callouts) renders normally.
- **Workaround:** Copy the Mermaid code block from the document and paste it into [mermaid.live](https://mermaid.live) to generate the image manually. Then paste the image back into the Word doc.
- **Bottom line:** This is cosmetic only. The content, steps, and decision logic are all still correct and complete. The document is fully usable without rendered diagrams.

### Markdown Formatting Rules (used by the build script)

- Clear heading levels (H1 for title, H2 for major sections, H3 for subsections)
- All Mermaid diagrams in fenced code blocks with ```mermaid language tag
- All screenshot placeholders as blockquotes starting with `**SCREENSHOT:`
- Pro tips as blockquotes starting with `**Pro Tip:**`
- At least one full worked example
- Page breaks between major sections using `---`
- Content at 5th-grade reading level

### Delivery

Save both the `.md` source file and the `.docx` output. Give the user the Word doc as the primary deliverable — this is what they'll share with their team. They can open it in Word or Google Docs to add actual screenshots wherever they see yellow placeholder boxes. Keep the `.md` as the editable source in case they want to regenerate the document after making changes.

## Investment Blueprint Definitions

When generating acquisition playbooks, these are the four blueprint types. Definitions match `src/playbook_generator.py`.

| Blueprint | Timeline | Exit Strategy | Key Detail |
|-----------|----------|---------------|------------|
| **Wholesale** | 7-14 day close | Assignment or double-close | No rehab, no ownership. Earnest money only. |
| **Flip** | 3-6 month cycle | Full rehab + retail sale | Highest profit and highest risk. $30K-$100K+ capital. |
| **Buy-and-Hold** | 30-60 day acquisition | Long-term rental | Cash flow + appreciation. $20K-$50K capital. |
| **Hybrid** | Mixed | Wholesale most, cherry-pick flips | Volume from wholesale, margin from flips. |

## Reference Files

- **Voice Guide**: [references/voice-guide.md](references/voice-guide.md) — Writing style and reading level rules
- **Process Mapping Guide**: [references/process-mapping-guide.md](references/process-mapping-guide.md) — How to build Mermaid flowcharts, decision trees, and swim lanes
- **Playbook Template**: [references/playbook-template.md](references/playbook-template.md) — Full template for playbook-style documents
- **SOP Template**: [references/sop-template.md](references/sop-template.md) — Full template for SOP-style documents
- **Screenshot Guide**: [references/screenshot-guide.md](references/screenshot-guide.md) — When and how to add screenshot placeholders
- **Foreclosure Example**: [references/foreclosure-example.md](references/foreclosure-example.md) — Complete worked example showing a transcript turned into an SOP
- **DOCX Build Script**: [scripts/build_docx.js](scripts/build_docx.js) — Converts Markdown with Mermaid into a formatted Word document
