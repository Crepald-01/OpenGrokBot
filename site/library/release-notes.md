---
name: release-notes
description: Write release notes for a version from merged changes, grouped and checked. Use before publishing a release.
status: draft
tags: dev, work
version: 1
---
# Release notes

## When to use
A new version is ready and the user wants user-facing release notes. Uses the workspace for the changelog source and the shared browser if the changes are listed on a web tracker.

## Inputs
- Version number and release date
- Source of changes: commit log export, merged pull request list, or the tracker's release view
- Audience (end users, developers, or both) and tone from `memory_search`

## Steps
1. Load the list of merged changes since the previous release tag.
2. Drop internal-only changes: CI, refactors with no behaviour change, dependency bumps without user impact.
3. Group the rest under: New, Changed, Fixed, Removed, Security.
4. Rewrite each item in one plain sentence describing the effect for the audience.
5. Mark breaking changes at the top with what the user must do.
6. Link each item to its issue or pull request number.
7. Save the draft to the workspace as `release-<version>.md`.
8. Show the user the draft and the list of changes you left out, with reasons.

## Checks
- Every breaking change has a migration step.
- Each item links to a real change in the source list.
- Version and date match what the user supplied.

## Needs approval
Publishing the notes to a website, GitHub release page, or newsletter, and creating the release tag itself.

## Edge cases
- Commit messages are unclear: ask the user for a sentence rather than guessing.
- Security fix: describe the impact without exploit details, and check with the user before naming the issue.
- No user-facing changes: say so and suggest a patch note only.
