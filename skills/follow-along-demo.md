---
name: follow-along-demo
description: How to learn a new job by watching the user do it once (Follow along), then turn it into a skill
status: active
tags: learning
---
# Learning from a demonstration

## When to use
The user says "let me show you" or a task is repetitive, site-specific, or has no API.

## Steps
1. Ask the user to open **Computer > Follow along** (or the Take over view) for you and press **Start recording**.
2. Tell them to do the job normally in the shared browser and to add short notes ("I always pick the second option", "skip rows marked void") with the note box.
3. When they press **Stop**, wait for the draft. Open it in **Skills**, read it critically and list anything unclear: inputs that change each time, steps that depend on page state, and where approval is needed.
4. Ask at most three clarifying questions, then update the draft with `skill_save`.
5. Suggest a **dry-run test** from the Skills page. Fix what the test shows.
6. Once the user activates the skill, offer to schedule it as a routine.

## Checks
- The skill names visible button/field labels and URLs, never CSS selectors.
- Secrets typed during the demo were not recorded; the skill says "the user logs in" instead.

## Needs approval
Any step that sends, submits, purchases, deletes or logs in must be marked in the skill.
