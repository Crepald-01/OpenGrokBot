---
name: lead-research
description: Build a sourced lead list and personalised outreach drafts for a target segment
status: active
tags: sales, outbound
---
# Lead research and outreach drafts

## When to use
The user gives a target segment (industry, size, region, trigger) and wants named leads with outreach.

## Inputs
- Segment definition and number of leads (default 25)
- The offer in one sentence and the user's voice (check memory)

## Steps
1. Restate the segment in one line and save it to the project note `outbound-<segment>`.
2. Search the web and company sites for matching companies. For each lead capture: company, URL, person and role, a specific trigger (hiring, funding, launch) and the **source URL** of that trigger.
3. Verify each claim on the source page. Drop leads whose trigger you cannot verify.
4. Write the leads to `shared/outbound/<segment>.csv` (columns: company, url, contact, role, trigger, source_url, notes).
5. Draft a short outreach email per lead that references the verified trigger. Keep to 90 words, one clear ask.
6. Save drafts to `shared/outbound/<segment>-drafts.md`. Do not send anything.
7. Summarise: lead count, how many were dropped and why, and the three strongest leads.

## Checks
- Every personalised line has a source URL in the CSV.
- No invented facts, titles or email addresses.

## Needs approval
Sending any outreach, importing leads into a CRM.
