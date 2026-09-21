---
name: doctor
description: SiftStack readiness check. Reports which installed SiftStack skills work right now, which need a login, which need an API key, what each key costs, and the no-API route to use instead. Use when someone asks "what can I run", "what do I need to set up", "why did that skill skip a step", "which keys do I need", "is SiftStack set up", "siftstack doctor", or right after installing any SiftStack plugin or bundle. Read-only, sends no requests, spends nothing, never prints a credential.
---

# SiftStack doctor

Answer one question: of the SiftStack skills, what runs today and what is blocked on what.

## Run it

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py"
```

Use `python3` if `python` is not on the PATH. Add `--env-dir <folder>` when the person keeps their `.env` somewhere other than the current folder, and `--json` when you need to reason over the result instead of showing it.

The script reads environment variables and a `.env` in the chosen folder. It records which NAMES are set and never reads a value into its output. You MUST NOT open or print the `.env` yourself, because the whole point of the script is that a credential never lands in the conversation.

## Report it

Give the person the three groups in this order, in plain words:

1. **Works right now.** These need nothing.
2. **Signed-in services.** Keys are fine, but the skill drives a site they must be logged in to. This cannot be checked from here, so say so instead of calling it ready.
3. **Needs a credential.** For each: the variable name, the real cost, and the route to use without it. A blocked skill with a fallback is not a dead end, so always say the fallback in the same breath.

Then offer the `setup` skill if anything is blocked and they want to unblock it. Do not push keys on someone whose work is covered by the first group.

## When it cannot run

No Python available (some Cowork sessions): read `${CLAUDE_PLUGIN_ROOT}/data/requires.json` directly. Each package lists `requires.tier` (`none`, `account`, `api`), `requires.env`, `requires.cost` and `requires.fallback`. Report tier `none` as working, and everything else as "needs X", since without the script you cannot see which variables are set.
