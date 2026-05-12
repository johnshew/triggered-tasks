---
agent: agency copilot
mode: plan
model: gpt-5.4
allow-tools:
  - shell
pre-processor: self-heal-log-alerts-prep.py
post-processor: self-heal-log-alerts-gh-issue.py
schedule: "0 * * * *"
timeout: 300
---

# Self-Heal Triggered Task Logs

Diagnose errors from triggered-task logs and create rich GitHub issues with
root-cause analysis, correlated timelines, and suggested remediations.

The pre-processor scans all JSONL logs for errors/warnings in the last UTC
hour, groups related findings by (task, phase, message prefix), filters out
groups that already have matching open GitHub issues (label: `self-heal`),
assembles correlated evidence for each group, and skips the agent when nothing
new is found.

When invoked, the agent receives grouped findings plus preassembled
`correlatedTimeline`, `slowRuns`, `gitContext`, and `similarIssues` data.
Use that evidence first. Only fall back to manual shell work if the
preassembled evidence is insufficient, and keep any fallback strictly
read-only and narrowly scoped.

## Pre-processor context

The pre-processor outputs JSON in the `pre-processor="..."` field:
```json
{
  "skip": false,
  "windowStart": "2026-04-22T13:00:00Z",
  "windowEnd": "2026-04-22T14:00:00Z",
  "totalFindings": 5,
  "totalGroups": 2,
  "untrackedGroups": 2,
  "groups": [
    {
      "task": "exercise-state-update",
      "phase": "agent",
      "messagePrefix": "timeout after 300s",
      "count": 3,
      "windowStart": "2026-04-22T12:57:00Z",
      "windowEnd": "2026-04-22T13:12:00Z",
      "correlatedTimeline": ["..."],
      "slowRuns": ["..."],
      "gitContext": ["..."],
      "similarIssues": [{"number": 42, "title": "...", "state": "CLOSED", "url": "..."}],
      "runOutput": [{"file": "task-20260422T130000Z-timeout.stdout", "content": "..."}],
      "findings": [...],
      "context": ["<jsonl lines leading up to error>"]
    }
  ]
}
```

## Diagnosis procedure

For each group in the pre-processor output, perform the following diagnosis.
Use the preassembled evidence first. Only if a group remains ambiguous after
reviewing that evidence may you use the triggered-task diagnostics
(`.claude/skills/triggered-task/docs/diagnostics.md`) and limited read-only shell
commands to verify one narrow point.

### 1. Review preassembled evidence

Start with the fields already provided by the pre-processor:

- `windowStart` / `windowEnd`
- `correlatedTimeline`
- `slowRuns`
- `gitContext`
- `similarIssues`
- `runOutput` — full stdout/stderr from agent runs near the error window
- `findings`
- `context`

Do not re-run the same queries unless the provided evidence is clearly
insufficient.

### 2. Optional manual verification

If you must verify something manually, keep it narrow and read-only.
Prefer `qlog.py`, `jq`, `grep`, `git log`, or `gh issue list` against the
specific task/window already provided by the pre-processor.

The `--errors` flag on `qlog.py` is especially useful — it auto-adds the
`error_category` column and prints a **Tracebacks** section with the last
20 lines of each traceback. Example:
```bash
uv run --script .claude/skills/triggered-task/scripts/qlog.py --errors --task exercise-state-update --since 2h
```

### 3. Identify known patterns

Check each group against these common patterns from the triggered-task diagnostics:

- **Stale-read race**: `duration_s > 60` on an agent run + multiple debounce
  batches on the same input file within that window.
- **Self-write cascade**: A second `start` within 1-2s of `done`, often with
  `status: skipped`.
- **Checkbox sync loop**: `exercise-sync-checkboxes` oscillation between
  Recommendations.md and Tracking.md.
- **Pre-processor gate failure**: `status: error` on `phase: pre-processor`.
- **Timeout**: `duration_s` near the `timeout` value in frontmatter.

Record the identified pattern name (or "unknown" if none match).

### 4. Classify severity

For each group, classify as one of:
- **noise** — deterministic warning repeated every run (same `line` content
  across multiple runs; use `COUNT(DISTINCT line)` to verify). Skip these.
- **transient** — one-off error that self-resolved (no recurrence in subsequent
  runs). Still file, but note it may not need action.
- **actionable** — genuine failure that needs investigation or a code fix.

### 5. Suggest remediation

Based on the pattern and git context, suggest a specific remediation:
- For stale-read races: "Add mtime guard to pre-processor" or "Reduce agent
  timeout window"
- For self-write cascades: "Exclude output files from watchPath"
- For timeouts: "Move deterministic work to pre-processor to reduce agent runtime"
- For unknown patterns: "Review transcript at <path> for agent reasoning"

## Output format

Respond with ONLY a JSON object, no other text:

```json
{
  "diagnosisDate": "2026-04-22",
  "window": "2026-04-22T13:00Z — 2026-04-22T14:00Z",
  "groups": [
    {
      "task": "exercise-state-update",
      "phase": "agent",
      "messagePrefix": "timeout after 300s",
      "severity": "actionable",
      "count": 3,
      "diagnosis": "Agent timed out during safety review. The pre-processor ran parse_tracking.py successfully but the agent exceeded the 300s timeout writing commentary.",
      "pattern": "Timeout — duration_s=302 near the 300s timeout threshold.",
      "correlatedTimeline": "13:02:41 exercise-state-update start trigger=cron\n13:07:43 exercise-state-update done status=error duration_s=302",
      "gitContext": "abc1234 2026-04-22 12:45 Add new exercise tracking entries",
      "remediation": "Consider increasing timeout to 400s or moving commentary generation to a post-processor.",
      "similarIssues": [
        {"number": 42, "title": "Self-heal: exercise-state-update timeout", "state": "CLOSED"}
      ],
      "findings": [{"ts": "...", "level": "error", "message": "..."}],
      "context": ["<jsonl lines>"]
    }
  ]
}
```

## Constraints

- Do NOT create GitHub issues directly — the post-processor handles that.
- Do NOT modify any files.
- Do NOT spend time rebuilding evidence already provided by the pre-processor.
- If you use shell, keep it read-only and limited to resolving a single unclear point.
- Output only the JSON diagnosis object.
- Classify deterministic repeated warnings as `"severity": "noise"` so the
  post-processor skips them.
- Keep `correlatedTimeline` concise — summarize, don't dump hundreds of lines.
