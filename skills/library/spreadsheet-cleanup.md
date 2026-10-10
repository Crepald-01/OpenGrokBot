---
name: spreadsheet-cleanup
description: Clean a messy spreadsheet: trim spaces, fix types, dedupe, and log every change. Use on request.
status: draft
tags: data, work
version: 1
---
# Spreadsheet cleanup

## When to use
The user shares a CSV or spreadsheet with duplicates, mixed date formats, stray spaces, or inconsistent category names and asks for it to be cleaned. Works on files in the workspace or a Drive sheet through the `drive` connector (request access if needed).

## Inputs
- Source file path or Drive file
- Columns the user cares about and any rules they gave (for example, "treat 'UK' and 'United Kingdom' as the same")
- Output location (default: a new file, original kept)

## Steps
1. Copy the source to a new file in the workspace. Never edit the original in place.
2. Profile each column: row count, blanks, distinct values, date formats, numeric parse failures.
3. Trim leading and trailing spaces; normalise case for category columns only where the user agreed.
4. Convert dates to ISO format (YYYY-MM-DD). Flag values that cannot be parsed; do not guess day and month.
5. Remove exact duplicate rows. For near-duplicates, list them and ask before removing.
6. Write a change log file listing each rule applied, the row count before and after, and each flagged cell.
7. Save the cleaned file and the change log to the workspace.

## Checks
- Row count before minus removed rows equals row count after.
- No numeric column lost values during conversion.
- Every removed or changed row appears in the change log.

## Needs approval
Overwriting the original file, deleting rows that are near-duplicates, or writing back to a shared Drive sheet.

## Edge cases
- Ambiguous dates such as 03/04/2025: flag, do not convert.
- Currency symbols mixed with numbers: split into an amount column and a currency column, keeping the original text.
- File too large for one read: work in chunks and say how many were processed.
