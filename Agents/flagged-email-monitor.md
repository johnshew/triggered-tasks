---
agent: agency copilot
mode: write
pre-processor: flagged-email-monitor-prep.py
schedule: "0 */2 * * *"
timeout: 300
env:
  WORKIQ_MAIL_TOKEN_CACHE_PATH: ~/.config/workiq-mail/.token-cache.json
---

# Flagged Email Monitor — Execute Now

**You are executing this monitoring job right now. Do not inspect, verify,
or manage any configuration files. Go straight to Step 1 and work through
each step in order.**

The pre-processor has already fetched email and Teams data into a
**context file** for each tracked item. The pre-processor output tells
you the file paths. **Do not make any MCP calls.** All the data you
need is in the context files.

## Important: file paths contain spaces

The `To Do/` directory tree contains spaces. Always use shell commands
(e.g. `find`, `grep`, `ls`) with proper quoting — **never** use glob
search tools for paths under `To Do/`.

## About context files

Each tracked email has a context file at
`Agents/data/flagged-email/<emailid>.md`. This is a **living document**
that the pre-processor appends to and you can annotate.

Structure:
- **## Metadata** — emailid, conversationId, fetch timestamps, item path
- **## Index** — one line per email/Teams message with ID, sender, date,
  and subject/snippet. You may add summary annotations to these lines.
- **## E:\<id\>** — detail section for each email message
- **## T:\<id\>** — detail section for each Teams message

The Index section lets you quickly scan what's there without reading
every detail section. When you need specifics, read the detail section
for that ID.

On each run, the pre-processor only appends new items it discovers.
Items already in the context file are not re-fetched or duplicated.

## Step 1. Read pre-processor output and files

The pre-processor output (in the `pre-processor` field) lists each
tracked item with:
- `item:` — path to the to-do file
- `context:` — path to the context file
- `new-emails:` — count of newly added email messages
- `new-teams:` — count of newly added Teams messages

For each item:
1. Read the **context file** — scan the Index first, then read detail
   sections as needed to understand the timeline.
2. Read the **to-do file** — the current Activity, Status, Recommended
   Next Action, and References sections.

## Step 2. Evaluate state

For each tracked item, determine what state it should be in based on the
full timeline of activity. Think about **whose court the ball is in**:

- **Action Required** (title prefix `Action Required -`): John needs to do
  something — reply, make a decision, follow up with someone.
- **Monitoring** (title prefix `Monitoring -`, set `pending: true`): John
  has done his part (acknowledged, delegated, responded) and is waiting for
  someone else. The system watches for new activity.
- **Completed** (`status: completed`): The thread is resolved — the ask has
  been fulfilled, the issue is closed, or both parties confirmed next steps.

Common patterns that mean John's part is done (→ Monitoring):
- John acknowledged and delegated to a colleague, who then responded
- John replied with a plan and the other party confirmed satisfaction
- John forwarded internally and someone else took the action

Common patterns that mean it should close (→ Completed):
- Thank-you reply with no further asks
- Meeting was scheduled and held
- Action items were completed and confirmed
- Thread went silent for 14+ days after resolution signals

If a new reply from the other party re-opens the thread (new question,
new ask, dissatisfaction) → flip back to Action Required.

If no activity for 72 hours since `lastChecked` and still in Monitoring →
append a staleness note.

## Step 3. Update files

For each item with new activity or state changes:

- Set `lastChecked` to the current date/time in `YYYY-MM-DD HH:MM ET` format
  in frontmatter (e.g., `lastChecked: 2026-04-15 10:30 ET`). Do NOT use ISO
  8601 / UTC format — use Eastern Time with the ET suffix.
- Update `pending`, `status`, and `title` in frontmatter as needed.
- Structure the file body in this order (create sections if missing):

  ### Section order

  1. **`## Status`** — 1-2 sentence summary of current state. Who has
     the ball, what they're doing, any blockers. Keep it scannable.
  2. **`## Recommended Next Action`** — what John should do (or not do)
     right now.
  3. **`## Activity`** — full chronological timeline.
  4. **`## Original Message`** — the triggering email body (moved here
     from the top of the file on first reformat; leave in place on
     subsequent runs).
  5. **`## References`** — machine-readable IDs for agent reuse.

  ### Status section

  ```markdown
  ## Status

  Monitoring. User delegated to Alice Smith (CVP) who responded
  with a plan. External contact confirmed satisfaction. Ball is in Alice/Bob's
  court. Alice OOO until April 20.
  ```

  ### Recommended Next Action section

  Based on the current thread state, write:
  - **Priority:** `urgent` / `standard` / `low` — based on staleness,
    sender importance, and whether the ball is in your court
  - **Action:** one concrete sentence describing what to do next
  - **Why:** brief rationale based on the timeline
  - **Deadline:** if there's a time-sensitive element, note it here

  This section should always reflect the *current* state — overwrite the
  previous recommendation each time.

  ### Activity section

  Replace the `## Activity` section (or `## Related Activity` if that
  exists from older formatting) with a **chronological timeline** of
  all activity found. The first entry should always be the triggering
  message that kicked this off, with a reference to the Original Message
  section below. Format rules:

  - **No numbering.** Use a bullet (`-`) for each entry.
  - **Sender name first, bolded.** Then what they did.
  - **Thread lane tag** at the end in italics — identifies the
    conversation thread. Use consistent short names:
    - `email — external-sender thread` (external email thread)
    - `email — internal` (internal forwards/replies)
    - `Teams — user/colleague 1:1` (Teams DM)
    - `Teams — Project LT group` (Teams group chat)
  - **Date/time at the end** in parentheses, not bold:
    `(Apr 8, 1:19 PM ET)`
  - **Do not add emojis.** Preserve any that appear in quoted content.
  - Order strictly by time. Include activity from **all channels**
    (email, Teams, documents, calendar).

  Example:

  ```markdown
  ## Activity

  _Updated Apr 15, 2026 10:30 AM ET_

  - **Dana Lee** (partner.example.com) sent the original escalation
    email. See [Original Message](#original-message) below.
    _email — Dana thread_ (Apr 8, 1:19 PM ET)
  - **User** forwarded internally requesting a draft response
    for review with Alice. _email — internal_ (Apr 8, 5:41 PM ET)
  - **Bob Chen** messaged User recommending they hold off replying
    to Dana — flagged complexity. _Teams — user/Bob 1:1_
    (Apr 8, 11:59 PM ET)
  - **Alice Smith** replied to Dana with a concrete adjusted
    plan. _email — Dana thread_ (Apr 14, 12:25 AM ET)
  ```

  ### Original Message section

  On the **first reformat only**, move the original email body text
  (everything between the frontmatter closing `---` and the first `##`
  heading) into a `## Original Message` section near the bottom. On
  subsequent runs, leave it in place.

  ### References section

  Add a `## References` section at the bottom linking to the context
  file and listing key IDs for human reference. The context file is the
  authoritative record — the References section is a human-readable
  summary.

  ```markdown
  ## References

  Context file: `Agents/data/flagged-email/<emailid>.md`

  ### Email

  - conversationId: `AAQkADA1NGI2ZjlhLWVl...full ID here...`
  - `AAMkADA1NGI2ZjlhLWVl...full ID...` — Dana Lee, Apr 8 (original)
  - `AAMkADA1NGI2ZjlhLWVl...full ID...` — Alice Smith, Apr 14 (RE)

  ### Teams

  - chatId: `19:aaa1234e00a000e0ab00e0ea0cb00e00@thread.v2` (Project LT group)
  - chatId: `19:00fa0000-b0cc-...@unq.gbl.spaces` (user/Alice 1:1)
  - messageIds: `1776157321070`, `1776099394502`, `1775851195537`
  ```

  On subsequent runs, **append new IDs** to the existing References
  section — do not remove previously discovered IDs.

- Write each file **once** — avoid repeated saves.

## Step 4. Check queued flagged emails

Run this shell command to count queued flagged emails:

```bash
for f in "To Do/Flagged Email/"*.md; do
  case "$(basename "$f")" in _index_.md|"_To Do_.md") continue;; esac
  [ -e "$f" ] || continue
  echo "$f"
done
```

Report how many queued emails exist and list their subjects.
Do NOT process or triage them.

## Step 5. Print summary

Print a final summary:
- How many email-sourced items were checked in `To Do/Active/`
- Any new activity found (from the pre-processor new-emails/new-teams counts)
- Any state changes made (pending→active, stale flags, completions)
- How many untriaged flagged emails exist in `To Do/Flagged Email/`

## Constraints

- Do NOT create new files — only update existing to-do files.
- Do NOT modify files with `status: completed`.
- Do NOT move or triage files from `To Do/Flagged Email/`.
- Do NOT read or modify any `Agents/*.md` configuration files.
- Do NOT make MCP calls — all data comes from the context files.
- You MAY read and annotate context files under `Agents/data/flagged-email/`.
- Use shell commands with proper quoting for all paths under `To Do/`.
