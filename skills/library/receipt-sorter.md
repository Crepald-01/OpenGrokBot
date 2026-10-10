---
name: receipt-sorter
description: Sort receipts from a folder or mailbox into a dated, categorised log for expense reports. Use monthly.
status: draft
tags: personal, data
version: 1
---
# Receipt sorter

## When to use
The user has receipts as files or emailed invoices and wants them logged by date and category, for an expense report or tax file. Uses the workspace folder and the `gmail` connector for emailed receipts (request access if needed).

## Inputs
- Receipt folder in the workspace or Drive
- Date range and category list from `memory_search` or the user
- Output log file (default: `receipts-<YYYY-MM>.csv`)

## Steps
1. List receipt files and matching emails for the range.
2. For each receipt, read the date, merchant, amount, currency, and tax if shown. Take values from the document itself.
3. Assign a category from the agreed list. Mark "uncategorised" when unsure.
4. Note the source path or email ID so each row can be found again.
5. Flag duplicates: same merchant, amount, and date.
6. Write one row per receipt to the log file. Keep original files where they are; copy nothing outside the workspace.
7. Report totals by category and the list of uncategorised or unreadable items.

## Checks
- Each amount matches the receipt, not an estimate.
- Totals in the report equal the sum of the rows.
- Currency is recorded for every row.

## Needs approval
Moving or renaming original receipts, deleting files or emails, submitting an expense claim, or sharing the log with an accountant or employer.

## Edge cases
- Blurry or handwritten receipt: mark "unreadable" and ask the user to type the amount.
- Receipt in a foreign currency: record the original amount and currency; do not convert without a stated rate.
- Receipts containing card numbers: record only the last four digits if the form requires any.
