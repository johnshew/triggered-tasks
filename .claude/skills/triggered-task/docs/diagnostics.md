# Log Diagnostics

Diagnose issues in the triggered-task pipeline (watchers -> pre-processor
-> agent -> post-processor) by correlating events across log files. Most
issues are races between a long-running agent and a fast watcher/sync
task writing the same files.

## Log inventory

All logs are **UTC JSONL**. There is one per-task log per entry in
`Agents/*.md`; the filename matches the task name
(`Agents/<name>.md` -> `Agents/logs/<name>.log`).

### Infrastructure logs

| Log | Type | What it tells you |
|-----|------|-------------------|
| `Agents/logs/triggered-tasks.log` | orchestrator | **Every** task's lifecycle: `watcher` debounce batches, `activate`, `start`, `done` with `status`, `duration_s`, `trigger`, `changed_files`. The join point for all diagnostics. |
| `Agents/logs/triggered-tasks-health-check.log` | cron `0 * * * *` | Hourly check that watchers and cron tasks are alive. Writes `Agents/health.ok` on success. |
| `Agents/logs/self-heal-log-alerts.log` | cron `0 * * * *` | Scans other logs for `level: error` / `status: error` and opens GitHub issues. First place to check for "something broke overnight". |
| `Agents/logs/git-sync.log` | cron | Commits and pushes generated files; pulls from remote. |

### Per-task logs (domain work)

| Log | Trigger | What it tells you |
|-----|---------|-------------------|
| `Agents/logs/note-index.log` | watch `.` | Rebuilds `_index_.md` files. Fires on **every** content write -- noisy. |
| `Agents/logs/dashboard-update.log` | cron `0 * * * *` | Regenerates the repo-root `DASHBOARD.md` from log aggregates. |
| `Agents/logs/todo-pull.log` | cron `0 * * * *` | Pulls from Microsoft To Do into `To Do/Active/`. |
| `Agents/logs/todo-push.log` | watch `To Do/Active/` | Pushes Active file changes up to Microsoft To Do. |
| `Agents/logs/flagged-email-sync.log` | cron `0 * * * *` | Hourly fetch of Outlook flagged emails. |
| `Agents/logs/flagged-email-monitor.log` | cron `0 */2 * * *` | Monitors Active items marked `pending: true`. |
| `Agents/logs/flagged-email-triage.log` | watch `To Do/Flagged Email/` | Fires the triage agent on new queue files. |

### Exercise system

| Log | Type | What it tells you |
|-----|------|-------------------|
| `Agents/logs/exercise-state-update.log` | cron + watch `Exercise/` | Main agent: staleness check, parse_tracking, judgment. Typical ~120-200s -- **the long-running agent that causes most races**. |
| `Agents/logs/exercise-sync-checkboxes.log` | watch `Exercise/Recommendations.md` | Post-run snapshot of checkbox state. **Best source for "who wrote which checkbox when".** |
| `Exercise/data/log/parse-tracking.log` | domain | Per-day parser results with `line_count / parsed_count / unparsed_count`. |

### Taskflow system

| Log | Trigger | What it tells you |
|-----|---------|-------------------|
| `Agents/logs/taskflow-agent.log` | file watcher | Full pipeline trace for every agent dispatch. |
| `Agents/logs/taskflow-handler.log` | watch `Taskflow/*/Active/`, `Taskflow/*/Monitoring/` | Checkbox detection, operation dispatch, debounce. |
| `Agents/logs/taskflow-monitor.log` | cron `*/30 * * * *` | Draft-sent detection, reply detection, state transitions. |
| `Agents/logs/taskflow-check-state.log` | cron `0 */4 * * *` | Validation, server reconciliation. |
| `Agents/logs/taskflow-email-sync.log` | cron `0 * * * *` | Flagged email fetch, category clearing. |
| `Agents/logs/taskflow-triage.log` | watch `Taskflow/*/Inbox/` | Triage agent runs on new inbox items. |
| `Agents/logs/taskflow-todo-sync.log` | cron `0 * * * *` | Microsoft To Do bidirectional sync. |

### Ancillary sources (not JSONL)

| Source | Use |
|--------|-----|
| `Agents/logs/runs/<task>-<ts>.stdout.txt` | Full raw stdout from every agent run. |
| `Agents/logs/runs/<task>-<ts>.stderr.txt` | Full raw stderr from every agent run. |
| `Agents/logs/runs/<task>-<ts>.transcript.md` | Archived session transcript (copilot/agency copilot). |
| `Agents/logs/<task>-transcript.md` | Live session transcript (overwritten each run). |
| `~/.agency/logs/session_*/chat.json` | Full Copilot agent transcript. |
| `git log --pretty='%h %ai %s' -- <file>` | Committed state history. |
| `crontab -l` | Active cron registrations (ground truth). |
| `ps aux \| grep inotifywait` | Active watcher processes. |

### Agent transcript deep dive

When a pipeline log shows an unexpected agent result, the session
transcript is the definitive source.

**Quick check -- run output files:**

```bash
# Recent runs for a specific task
ls -lt Agents/logs/runs/exercise-judgment-* | head -10

# View the stdout of the most recent run
cat "$(ls -t Agents/logs/runs/exercise-judgment-*.stdout.txt | head -1)"
```

**Locating the right session (agency copilot):**

```bash
ls -lt ~/.agency/logs/ | head -15

for d in $(ls -dt ~/.agency/logs/session_*/ | head -20); do
  grep -ql 'Escalation\|ISE' "$d"/*.json 2>/dev/null && echo "$d"
done
```

**Correlating with pipeline logs:**

The `phase: agent` log entry includes `transcript_path` and the session
directory in its `message` field. To go from a log entry to the full
transcript:
1. Find the `phase: agent` entry for your task/time
2. Extract the session directory from the `message` field
3. Read `chat.json` in that directory

## Diagnostic procedure

1. **Identify the symptom and approximate time.** Convert to UTC.

2. **Get a narrow window from `triggered-tasks.log`:**
   ```bash
   uv run --script .claude/skills/triggered-task/scripts/qlog.py \
     --task <name> --since <time> --until <time>
   ```
   Look for: `start (trigger=...) -> done (status=ok|error|skipped, duration_s)`.

3. **Find the long-running agent.** `duration_s > 30` is the usual suspect.

4. **Pull the per-task log** for that window.

5. **Correlate with domain logs.** For exercise issues,
   `parse-tracking.log` shows what the parser saw at run time.

6. **Cross-check with git.** `git log --pretty='%h %ai %s' -- <file>`.

## Common patterns

- **Stale-read race.** Long agent reads input at t=0, writes output at
  t=200s. User edits during that window. Signature: `duration_s > 60`
  + multiple debounce batches on the same input file.

- **Self-write cascade.** Agent writes back to a watched input file.
  Signature: a second `start` within 1-2s of `done`, often with
  `status: skipped` (pre-processor catches the self-write).

- **Checkbox sync loop.** `exercise-sync-checkboxes` propagates check
  state between Recommendations.md and Tracking.md. When the agent also
  writes one of those files, you get oscillation.

## Traps to avoid

- `tail -N` / `cat` on 200k-line logs overflows context. Always filter
  first with qlog.py or timeline.py.
- **Table display caps columns at 80 chars.** Use `--format jsonl` for
  full values.
- File-change events don't mean content changed -- mtime can bump on an
  identical atomic-write.
- `triggered-tasks.log` has **multiple tasks interleaved**. Filter by
  `--task` or `"task":"<name>"`.
- **Every log entry has a `task` field** (backpatched 2026-04-26).
- **Schema version.** Entries carry `log_schema: <int>` (current: 3).
  When DuckDB crashes on `UNION ALL BY NAME` with a type mismatch,
  **fix the data, not the query** -- see
  [commands.md](commands.md) "Schema evolution".
- **`--sql` ignores other filter flags.** Include conditions in your SQL
  `WHERE` clause.
- **Warnings are deterministic telemetry.** 85 warnings in 12h = 6 runs
  x ~14 repeated warnings. Group by `COUNT(DISTINCT line)`.
- **Level vs status.** Some handlers report errors as `level: error`,
  others as `status: error`. The `--errors` filter ORs both.
- **`--errors` auto-enriches output** with `error_category` column and
  a Tracebacks section.

## Query recipes

### qlog.py

```bash
S=.claude/skills/triggered-task/scripts

# Events for one task in a window
uv run --script $S/qlog.py --task exercise-state-update \
       --since 2026-04-22T13:00 --until 2026-04-22T13:30

# Correlated view across ALL logs
uv run --script $S/qlog.py --all --since 2026-04-22T13:02:41 --until 2026-04-22T13:06:10 \
       --columns ts,_src,task,phase,status,duration_s

# Slow runs (agent duration > 30s)
uv run --script $S/qlog.py --slow 30 --since 2026-04-22

# All errors across all logs
uv run --script $S/qlog.py --errors --all

# Custom SQL
uv run --script $S/qlog.py --all --sql "SELECT task, COUNT(*) FROM log GROUP BY 1 ORDER BY 2 DESC"
```

Filters: `--task`, `--since`, `--until`, `--phase`, `--status`, `--trigger`,
`--slow SEC`, `--errors`. Output: `--format table|jsonl|csv`.

### timeline.py

```bash
# Timeline for a specific task
uv run --script $S/timeline.py exercise-state-update --since 2026-05-01T12:00

# Content substring filter
uv run --script $S/timeline.py LMCO --systemd --last 30

# All tasks in a window
uv run --script $S/timeline.py --all --since 2026-05-01T16:00

# Include systemd journal
uv run --script $S/timeline.py flagged-email-triage --systemd
```

### jq (quick filters without DuckDB)

```bash
grep '"task":"exercise-state-update"' Agents/logs/triggered-tasks.log \
  | tail -50 | jq -c '{ts,phase,status,trigger,duration_s,changed_files}'
```

## Key files for reference

### Exercise pipeline
- Pre-processor: `Agents/handlers/exercise-state-prep.py`
- Parse entry point: `Exercise/scripts/parse_tracking.py`
- Sync post-processor: `Exercise/scripts/exercise_sync_checkboxes.py`

### Taskflow pipeline
- Task definition: `Taskflow/agents/taskflow-agent.md`
- Pre-processor: `Taskflow/agents/handlers/taskflow-agent-prep.py`
- Post-processor: `Taskflow/agents/handlers/taskflow-agent-apply.py`
- Handler: `Taskflow/agents/handlers/taskflow-handler.py`
- Email sync: `Taskflow/agents/handlers/taskflow-email-sync.py`
