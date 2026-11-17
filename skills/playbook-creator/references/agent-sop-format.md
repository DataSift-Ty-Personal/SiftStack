# Agent SOP Format (.sop.md)

An agent SOP is a markdown file an AI agent can execute directly. The human SOP tells a person how to do the job. The agent SOP tells an agent how to do the same job: what inputs it needs, what to do at each step, and the exact rules it must follow. The format follows the open Agent SOPs standard (strands-agents/agent-sop), so any file you produce here also works as an MCP prompt, a Claude skill, or a system prompt without changes.

Write one whenever the SOP's steps are things an agent could do or help with: pulling data, filtering records, updating a CRM, running research, building a report. Skip it only when the process is purely physical (walking a property, taking courthouse photos).

---

## The Four Structural Invariants

Every agent SOP MUST have these four things. The validator checks all of them.

1. **Filename ends in `.sop.md`**, kebab-case: `pull-foreclosure-records.sop.md`
2. **An `## Overview` section** right after the H1 title. It doubles as the SOP's description everywhere it gets listed, so it must be self-contained prose: what it does and when to use it, in 2-4 sentences.
3. **A `## Parameters` section** with a `**Constraints for parameter acquisition:**` block.
4. **A `## Steps` section** with numbered `### 1. Step Name` headings, each carrying a `**Constraints:**` block.

`## Examples` and `## Troubleshooting` are recommended but optional.

## Document Skeleton

```markdown
# [SOP Name]

## Overview

[2-4 sentences: what this does, what it produces, when to use it.]

## Parameters

- **param_name** (required): [What it is and where to get it]
- **other_param** (optional, default: "value"): [What it is]

**Constraints for parameter acquisition:**
- If all required parameters are already provided, You MUST proceed to the Steps
- If any required parameters are missing, You MUST ask for them before proceeding
- When asking for parameters, You MUST request all parameters in a single prompt
- When asking for parameters, You MUST use the exact parameter names as defined

## Steps

### 1. [Step Name]

[Plain-language description of what happens in this step.]

**Constraints:**
- You MUST [specific requirement]
- You SHOULD [recommended behavior]
- You MAY [optional behavior]

### 2. [Next Step]

[...]

## Examples

### Example Input
[A realistic parameter set]

### Example Output
[What a finished run produces]

## Troubleshooting

### [Common Problem]
[What it looks like and what to do]
```

## Parameter Rules

- Names are `snake_case`, lowercase only. Steps reference them as `{param_name}` placeholders in plain text (`write results to {output_dir}/report.md`). There is no templating engine; the agent resolves them.
- Required parameters are listed before optional ones.
- Optional parameters state their default inline: `(optional, default: "output/")`.
- Every description says where the value comes from, not just what it is. "The county to pull (Knox or Blount)" beats "the county".
- The four acquisition constraints above are the mandatory baseline. Add input-method constraints when a parameter can arrive as a file path, URL, or pasted text.

## Constraint Rules (RFC 2119)

Constraints are the runtime. There is no engine; the agent obeys the constraint text. Always second person:

| Keyword | Meaning | Use for |
|---------|---------|---------|
| You MUST | Absolute requirement | Anything that breaks the process if skipped |
| You MUST NOT | Absolute prohibition | Anything destructive or misleading |
| You SHOULD | Strong recommendation | The right default, override with reason |
| You MAY | Truly optional | Nice-to-haves |

**Every negative constraint carries a reason.** Write "You MUST NOT [action] because [reason]". A prohibition without a reason gets ignored or misapplied.

Good:
```
- You MUST NOT mark a record sold based on the tag index alone because the index lags the record detail by about 30 seconds
- You MUST NOT invent a phone number or fill an unverified field because someone on the team will dial what you write
```

Bad:
```
- You MUST NOT skip validation
```

**Do not mix requirement levels.** If a behavior is optional, it gets MAY, not MUST. If everything is MUST, nothing is.

## Conditional Steps

Put the condition in the step description AND in the constraints:

```markdown
### 3. Enrich Records

If the record count is above zero, enrich each record. Otherwise, report the empty result and stop.

**Constraints:**
- You MUST check the record count before proceeding
- If the count is zero, You MUST report it as a failure and stop because a silent empty run reads as success and hides a broken pull
- If the count is above zero, You MUST enrich every record before moving to Step 4
```

## Interactive Steps

When a step needs back-and-forth with the user, the constraints say how to run the conversation and where to save it:

```markdown
**Constraints:**
- You MUST ask one question at a time
- You MUST append each question and answer to "intake-notes.md"
- You MUST continue until you have [the specific things needed]
```

## Progress Artifacts (Resumability)

Every step that produces something names the exact file path it writes to. This is the resumability story: a broken run can be picked up by reading the artifacts. Rules:

- Specify file paths for ALL created artifacts, in the constraints, using `{param}` placeholders where the path depends on input.
- Long or multi-session SOPs MUST keep a `progress.md`: what step is done, what was decided, what is next.
- A step that makes a judgment call MUST record the call and the reason in the artifact, not just the outcome.

## Authoring Loop

1. Draft the `.sop.md`.
2. Run the validator: `python <skill-path>/scripts/validate_sop.py my-process.sop.md`
3. Fix every error. Read every warning and fix or consciously accept it.
4. Re-run after EVERY edit. The loop is cheap on purpose.

The validator checks: the `.sop.md` extension, the H1 title, Overview / Parameters / Steps sections, the parameter-acquisition block, numbered step headings, at least one Constraints block, RFC 2119 keywords present, and reasons on negative constraints.

## Keep Steps Atomic

One clear objective per step. If a step description needs the word "then" three times, it is two steps. Minimize nested conditionals: a step with more than two IF branches usually wants to be a decision table in the human SOP and a lookup step here.

## What NOT to Put in an Agent SOP

- General best practices a competent agent already knows (they add tokens, not behavior)
- Volatile numbers that go stale (record counts, file sizes)
- Made-up acronyms or field names not verified against the real system
- Credentials or API keys (parameters name the env var, never the value)
