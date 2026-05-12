---
agent: agency copilot
mode: write
timeout: 400
pre-processor: flagged-email-triage-prep.py
watchPath: To Do/Flagged Email/
watchIgnore:
  - _index_.md
  - _To Do_.md
mcps:
  - workiq-cli
  - mail
env:
  WORKIQ_MAIL_TOKEN_CACHE_PATH: ~/.config/workiq-mail/.token-cache.json
---

# Flagged Email Triage

Watches `To Do/Flagged Email/` for new email files created by the
`flagged-email-sync` handler. For each new email, gathers context, drafts a
response plan, asks Copilot to review that plan, re-checks context with
WorkIQ, and then creates a single actionable to-do with drafted responses.

## Pre-processor context

The pre-processor has already fetched email data via MCP. The
`pre-processor="..."` field contains JSON:
```json
{
  "files": [
    {
      "source_file": "To Do/Flagged Email/some-email.md",
      "frontmatter": {"emailid": "...", "conversationId": "...", "title": "...", "from": "...", "received": "..."},
      "existing_active_item": "To Do/Active/Action Required - Subject.md",
      "thread_message_count": 5,
      "thread_messages": [{"id": "...", "receivedDateTime": "...", "from_name": "...", "subject": "...", "bodyPreview": "..."}],
      "trigger_body": "Full body of the triggering email in markdown",
      "mcp_available": true
    }
  ]
}
```

**Use the pre-fetched data first.** Only make additional MCP calls for:
- Sender/relationship lookups via WorkIQ (`ask_work_iq`)
- Related to-do searches that need more context than `existing_active_item`
- Category removal in Step 7 (requires `mail`)

The `thread_messages` array has the full thread preview (sender, date,
subject, bodyPreview). The `trigger_body` has the full body of the
triggering email already converted to markdown.

## Important: file paths contain spaces

The `To Do/` directory tree contains spaces. Always use shell commands
(e.g. `find`, `grep`, `ls`, `cat`) with proper quoting — **never** use glob
search tools for paths under `To Do/`.

## MCP tools available

You have two MCP connections. Use the right one for each task:

| MCP | Capability | Use for |
|-----|-----------|--------|
| `workiq-cli` (`ask_work_iq`) | **Read-only** — search emails, calendar, files, Teams messages | Steps 2, 3, 5: gathering context, thread history, sender info |
| `mail` | **Read + write** — read email details, update categories, manage messages | Step 7: reading current categories and removing the import category |

`ask_work_iq` (workiq-cli) **cannot modify emails**. To update categories you
**must** use `mail` tools directly (e.g.
`GetMessageDetails`, `UpdateMessageCategories`, or the appropriate
tool exposed by that server).

## When triggered

A new `.md` file appears in `To Do/Flagged Email/`. This happens when the
hourly `flagged-email-sync` cron task fetches a newly flagged email.

## Steps

### 1. Identify the changed file

Read the pre-processor JSON from the `pre-processor="..."` field. For each
file in the `files` array:

- Use `frontmatter` for emailid, conversationId, title, from, received
- Check `existing_active_item` — if set, an Active item already exists for
  this thread. Do not create another one; update the existing one if needed.
- Use `thread_messages` for the email thread history (already sorted
  chronologically with sender, date, subject, bodyPreview)
- Use `trigger_body` for the full body of the triggering email

If the pre-processor could not fetch data (`mcp_available: false`), fall back
to reading the source file and making MCP calls directly.

### 2. Gather context via WorkIQ

Using the email metadata from the file's frontmatter (`from`, `emailid`,
`conversationId`, `title`):

1. **Retrieve the full email thread** — use WorkIQ to look up the
   conversation history, prior replies, and any attachments. Prefer
   `conversationId` when available; fall back to `emailid` / subject only when
   `conversationId` is missing.
2. **Identify the sender** — role, department, and recent interactions.
3. **Find related to-dos** — scan files in `To Do/Active/` for items from
   the same sender or related to the same project/thread.

### 3. Draft the initial plan

As the primary drafter (`agency claude`), produce an initial response plan:

1. Classify urgency as one of:

   | Level | Criteria |
   |-------|----------|
   | `quick-win` | Actionable in < 10 minutes (simple approvals, acknowledgements, one-line replies) |
   | `urgent` | VIP sender (manager, skip-level, executive) or time-sensitive (deadline within 24h, meeting conflict) |
   | `standard` | Everything else |

2. Draft the proposed action checklist.
3. Draft any email responses as blockquotes tied to the relevant checkbox.
4. Summarize the context that supports the plan.

### 4. Get a second-agent review before writing files

Before creating or updating any `To Do/Active/` file, ask Copilot to review
the proposed plan.

- Preferred reviewer: `agency copilot`
- Acceptable fallback: `copilot`

Ask the reviewer to look for:

- missing context from the thread or sender history
- duplicate or conflicting existing to-dos
- over-committing language in the drafted response
- missing follow-ups, stakeholders, or deadlines
- urgency misclassification
- **whether the thread is already resolved** and no Active item should be
  created

**Blocking feedback is mandatory.** If the reviewer flags an issue as
"blocking", you **must** address it before proceeding. In particular:

- If the reviewer says **do not create an Active item** (e.g., thread is
  already resolved, work is complete, no action needed), then **skip step 6**
  entirely. Proceed directly to step 7 (remove category and delete queue
  file) without creating or updating any Active item.
- If the reviewer says the urgency is wrong, fix it.
- If the reviewer says proposed actions are unsupported by evidence, remove
  them.

Non-blocking feedback should be incorporated where reasonable.

### 5. Re-check WorkIQ context

After the second-agent review, run one more focused WorkIQ check for any gaps
the reviewer surfaced. Only proceed once the plan reflects both the secondary
review and the latest WorkIQ context.

### 6. Create or update exactly one thread action item

**Skip this step entirely** if the second-agent review determined that no
Active item is needed (e.g., thread is resolved, no action required). Go
directly to step 7.

If no matching Active item already exists, create a new file at
`To Do/Active/Action Required - <normalized thread subject>.md` with:

**Frontmatter:**
```yaml
---
title: "Action Required - <email subject>"
status: notStarted
source: email-triage
flagged-email: true
urgency: <quick-win|urgent|standard>
emailid: <original email id from source file>
conversationId: <thread conversationId when available>
createdBy: flagged-email-triage
createdAt: <current date/time in "YYYY-MM-DD HH:MM ET" format>
---
```

**Body:**
- A heading with the email subject
- A context line: sender, received date, urgency with emoji
  (🟢 quick-win, 🔴 urgent, 🟡 standard)
- A `## Creation Context` section explaining how this item was created:
  - Which flagged email triggered it (filename and received date)
  - How many other emails in the same thread were found in the flagged queue
  - Key context gathered from WorkIQ (thread participants, timeline)
  - What the second-agent reviewer flagged, if anything
  - This section helps downstream agents (like flagged-email-monitor)
    understand the full history without re-querying WorkIQ from scratch
- An `## Actions` section with a checkbox list (`- [ ]`) of concrete steps
- Draft email responses as blockquotes (`>`) after the relevant checkbox
- Other actions as appropriate: schedule meetings, loop in colleagues,
  update documents, create follow-up to-dos
- An Outlook link for the email or thread so the user can jump back to the
  source conversation after the flagged-email queue file is removed
- A `## Feedback Signals` section so the user can record recommendation
  quality after acting on it. Include checkboxes for:
  - `- [ ] Used the recommended plan substantially as written`
  - `- [ ] Sent the drafted response with only light edits`
  - `- [ ] Needed major changes to the recommendation`
  - `- [ ] Important context was missing`
  - `- [ ] Recommendation created unnecessary work`
  Treat the first two as positive quality signals when they are later checked.
  Use this exact template:

  ```markdown
  ## Feedback Signals

  - [ ] Used the recommended plan substantially as written
  - [ ] Sent the drafted response with only light edits
  - [ ] Needed major changes to the recommendation
  - [ ] Important context was missing
  - [ ] Recommendation created unnecessary work
  ```

- The full flagged email body should be included **inside the
  `## Original Message` section**, right after the summary paragraph and
  Outlook link, separated by a horizontal rule (`---`). Fetch the full
  body of the triggering email via `GetMessage` (without `bodyPreviewOnly`)
  and convert HTML to clean markdown. This gives the user and downstream
  agents full context inline. Use this layout:

  ```markdown
  ## Original Message

  <summary paragraph>

  [Open in Outlook](<outlook link>)

  **From:** <sender>
  **Date:** <date>
  **Subject:** <subject>

  <plain text body of the email, HTML stripped>
  ```

- A `## Processing Log` section as the **last section** recording what
  happened during triage. Use this template:

  ```markdown
  ## Processing Log

  - **<current date/time in YYYY-MM-DD HH:MM ET>** — Created by flagged-email-triage from `<source filename>`
  ```

  Future workflow steps (todo-push sync, monitor checks, manual edits) will
  append entries to this section.

Important constraints while writing:

- Create or maintain **at most one** `To Do/Active/` item per email thread.
- Do all planning and review **before** writing the Active file.
- If an Active file with the same `conversationId`, `emailid`, or normalized
  thread subject appears during your work, reuse that file instead of creating
  another one.
- Do not edit the Active file repeatedly after creation or update, because each extra
  save can retrigger downstream automations such as `todo-push` — write the
  final version once.

### 6b. Create the initial context file

After creating or updating the Active file, seed a context file for the
flagged-email-monitor at:

```
Agents/data/flagged-email/<emailid>.md
```

Create the directory if it doesn't exist. Write the file with:

```markdown
---
emailid: <emailid>
conversationId: <conversationId if available>
subject: <normalized thread subject>
participants: <comma-separated list of thread participants>
createdAt: <current date/time in YYYY-MM-DD HH:MM ET>
createdBy: flagged-email-triage
---

## Index

- E:<short-emailid> — <sender name>, <received date>, <subject>

## Thread Summary

<Brief 2-3 sentence summary of the thread context gathered during triage>
```

This gives the monitor pre-processor a warm cache so it doesn't have to
re-fetch the full thread from scratch on its first run.

### 7. Remove the import category, then delete the queue file

This step runs whether or not an Active item was created (it also runs
for cleanup-only dispositions where the reviewer said no Active item is
needed).

1. Use **`mail`** (not `ask_work_iq`) to read the current
   categories on the source email identified by `emailid`. The `ask_work_iq`
   tool is read-only and cannot modify emails.
2. Remove only the import category `Action Needed | To Do`.
3. Preserve every other server-side category exactly as-is.
4. Only after the category update succeeds, delete the source file from
   `To Do/Flagged Email/`.

If the server-side category update fails, do **not** delete the source file.
Leave it in place so the workflow can be retried safely.

## Important

- Do NOT create a second Active item for the same thread.
- Do NOT watch or write inside `To Do/Active/` until the final action item is
  ready; this watcher only exists for `To Do/Flagged Email/`.
- If WorkIQ is unreachable, log the error and exit — the next watcher
  trigger will retry.
- If the Copilot review step cannot be completed, stop without creating a new
  Active to-do or modifying an existing one.
- Keep draft responses professional and concise. Use placeholders
  (e.g., `[approve / request changes]`) where the user needs to make a
  decision.
