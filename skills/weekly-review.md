---
name: weekly-review
description: Weekly review of all Bots' work: what shipped, what is stuck, what needs the user's decision
status: active
tags: team, weekly
---
# Weekly review (chief of staff)

## When to use
Every Friday afternoon (good candidate for a routine: `0 16 * * 5`) or when the user asks for a status report.

## Steps
1. `bots_list` and `handoff_list status=all`.
2. For each Bot, `recall_conversations` for the week's work summaries and note outcomes.
3. Group findings: **Done**, **In progress**, **Blocked or stalled** (no update > 4h counts as stalled), **Needs your decision**.
4. For each stalled handoff: nudge the owner with `message_bot`. If the owner is stuck, re-assign with `handoff_create` and say so.
5. Update the shared project note `weekly-review` with the report (append, newest on top).
6. Send the user a short summary: three bullets of wins, the blockers with owners, and the decisions you need, each with your recommendation.

## Checks
- Each claim names the Bot and the source (handoff id, project note, file).
- Anything marked "done" was verified at the source, not just reported.

## Needs approval
None. This skill only reads and writes notes. Sending a message outside the app needs approval.
