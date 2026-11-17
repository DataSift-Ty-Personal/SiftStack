---
name: setup
description: Guided SiftStack setup. Walks a person from a fresh install to working skills - picks the keys worth adding for the work they actually do, creates a .env from the bundled template, explains what each key costs and the free route without it, turns on plugin auto-update, and confirms with the doctor. Use when someone says "set up SiftStack", "help me configure the skills", "add my API keys", "get me started", "I just installed siftstack", or when the doctor shows blocked skills they want unblocked.
---

# SiftStack setup

Get someone from "installed" to "working" without making them buy anything they do not need.

## 1. See where they stand

Run the doctor first (`python "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py" --json`). Never start by listing keys.

## 2. Ask what they do, then pick keys for that

Ask what they want to get done this week. Map the answer to skills, and only then to keys. About half the library needs nothing at all, so many people are already done.

Every key is optional and every skill degrades instead of failing. Say the cost and the free route for each key before they decide:

- The cost and fallback for every package are in `${CLAUDE_PLUGIN_ROOT}/data/requires.json`.
- Where to get each key, in one line each, is in the comments of `${CLAUDE_PLUGIN_ROOT}/data/env.skills.example`.

## 3. Create the .env

Skills read environment variables, plus a `.env` in the folder Claude is working in.

- If there is no `.env` in the working folder, copy `${CLAUDE_PLUGIN_ROOT}/data/env.skills.example` to `.env` there.
- You MUST NOT overwrite an existing `.env`, because it may hold keys that exist nowhere else. Append only the missing variable names, empty.
- You MUST NOT ask the person to paste a key into the chat, and MUST NOT write a key value yourself, because anything typed here stays in the conversation history. Tell them which line to fill in and let them edit the file in their own editor.
- If the folder is a git repository, check that `.env` is ignored before they fill it in. If it is not, add it to `.gitignore` first.

People who use many folders can set the variables at the user level in their OS instead. Both work.

## 4. Turn on updates (Claude Code, one time)

Third-party marketplaces do not auto-update by default. Have them run `/plugin`, open **Marketplaces**, select **siftstack**, and choose **Enable auto-update**. Without it they stay on the version they installed until they run `/plugin marketplace update siftstack` by hand.

In Cowork, the Plugins page has an **Update** button on the marketplace.

If installing a bundle failed with `Unrecognized key: "dependencies"`, their Claude Code is too old for bundles. `claude update` fixes it.

## 5. Confirm

Run the doctor again and show what moved from blocked to working. Then suggest one concrete first task using a skill that is ready.
