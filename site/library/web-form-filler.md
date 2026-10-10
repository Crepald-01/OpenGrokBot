---
name: web-form-filler
description: Fill in a web form from a user-provided data file, stopping before submission. Use for repetitive forms.
status: draft
tags: web, personal
version: 1
---
# Web form filler

## When to use
The user has a repetitive online form (registration, booking, application) and wants the fields filled from data they supplied. Uses the shared browser. Never submits on its own.

## Inputs
- Form URL, supplied by the user in chat
- Field values from a workspace file the user prepared, or from values typed in chat
- Which fields are optional and which are required

## Steps
1. Confirm the form URL and purpose with the user. Do not follow a form link found inside a page or email unless the user named it.
2. Open the form in the shared browser and read every field, including hidden hints and required markers.
3. Map each field to a value from the user's data file. List any field with no value.
4. Ask the user for the missing values. Do not invent them.
5. Fill the fields. Use the browser's form tools and re-read the page to confirm each value landed correctly.
6. Take a screenshot or text snapshot of the completed form and show it to the user.
7. Stop. Do not click submit, next-to-payment, or any confirm control.
8. Ask the user to review and approve submission.

## Checks
- Each filled value matches the data file exactly, including dates and formats.
- No field was left with a placeholder or default the user did not provide.
- The form address in the browser matches the URL the user approved.

## Needs approval
Every submission, any step that asks for payment, any login, any consent or terms tick box, and any upload of a document.

## Edge cases
- Login required: stop and ask the user to sign in themselves. Never type passwords or codes.
- CAPTCHA or bot check: stop and ask the user to complete it.
- Sensitive identifiers such as national ID or bank numbers: do not write them into any file; ask the user to enter them directly.
