---
name: invoice-chaser
description: Find unpaid invoices past due and draft polite payment reminders. Use weekly or when asked to chase.
status: draft
tags: work, email
version: 1
---
# Invoice chaser

## When to use
The user asks to chase overdue invoices, or runs this weekly. Needs the `gmail` connector and the `drive` connector or the shared workspace where invoice copies are kept (request access if missing).

## Inputs
- Invoice register (spreadsheet in the workspace or Drive) with invoice number, client, amount, due date, status
- Reminder tone and escalation steps from `memory_search`
- Contact list for each client

## Steps
1. Load the invoice register. If none exists, ask the user to point to one and stop.
2. Mark invoices as overdue when the due date is before today and the status is not paid.
3. For each overdue invoice, search mail for the invoice number to see whether the client has already replied.
4. If there is a recent reply saying payment is coming, do not draft a chaser. Note it instead.
5. Draft a reminder per client in the user's usual tone: invoice number, amount, original due date, one clear request.
6. Create each draft with `create_draft`. Never put bank details in a draft unless they are already in the invoice itself.
7. Call `followup_schedule` for 7 days after each draft.
8. Return a table: client, invoice, amount, days overdue, draft status.

## Checks
- Amounts and dates match the invoice file, not memory.
- Each draft goes to the billing contact on record.
- Currency symbols are right for each client.

## Needs approval
Sending any reminder, adding late fees or interest, changing invoice status in the register, or contacting a client the user has not named.

## Edge cases
- Partial payments: chase the remaining balance only.
- Client disputes in mail: stop chasing and flag the thread to the user.
- Missing due date: mark "unknown" and do not chase.
