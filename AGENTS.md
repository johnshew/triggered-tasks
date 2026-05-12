# AGENTS

## Load before acting

| When you are...                                     | Read first                               |
|-----------------------------------------------------|------------------------------------------|
| Doing any exercise work (logging, recs, audits)     | `.agents/exercise.md`                    |
| Working on triggered agents (create, edit, debug)   | `.agents/triggered-tasks.md`             |
| Handling user tasks, to-dos, "what should I work on" | `.agents/To Do Task.md`                  |
| Diagnosing pipeline issues (missing updates, errors, handler failures) | `.claude/skills/triggered-task/docs/diagnostics.md` |
| Diagnosing taskflow issues (checkboxes, triage, flush timer) | `.claude/skills/review-taskflow-state/SKILL.md` |
| Writing or testing a triggered task                 | `.claude/skills/triggered-task/SKILL.md` |
| Acting within any other skill's domain              | `.claude/skills/<name>/SKILL.md`         |

## DO NOT

- **DO NOT manually parse log files.** Use the triggered-task `logs` commands (`qlog.py`, `timeline.py`). They correlate events across log files and agent transcripts. Manually reading `Agents/logs/*.log` or `triggered-tasks.log` is unreliable and has repeatedly led to wrong conclusions.
- **DO NOT edit Exercise files directly.** Use the exercise skills (`/exercise-log`, `/exercise-next`, `/exercise-audit`). Only `Exercise/Tracking.md` may be hand-edited.
- **DO NOT hand-edit auto-generated files** (`_index_.md`, `Agents/data/todo-index.json`, `Exercise/Current State.md`).
- **DO NOT use `git checkout`, `git reset`, or `git stash`** on tracked files. Other agents run concurrently and may have uncommitted work. Re-edit the file instead.
- **DO NOT run triggered-task commands in the VS Code sandbox.** Commands `status`, `run`, `start`, `stop`, `teardown` require `requestUnsandboxedExecution: true`. The sandbox blocks `crontab` and process detection.
- **DO NOT add unit tests or fixture tests.** The pipeline's own output and self-healing loop catch regressions. See `.agents/testing.md` if you think a test is warranted.
- **DO NOT add backward-compatibility shims.** Clean break, migrate all consumers. Ask the developer before adding any compat code.

## Rules

- **Check `Agents/data/health.ok` is less than 1h old** before any work in this repo. Triggered tasks handle indexing, git sync, exercise state, and task management - nearly everything depends on them. If stale or missing, read `.agents/infrastructure.md` for diagnosis and recovery.
- Read before writing. Understand the existing file and its neighbors first.
- Minimal diffs. Change only what was asked for. Preserve style.
- Standard markdown links (not `[[wikilinks]]`). Relative paths. Don't fabricate links.
- Local time with timezone in generated files (e.g. `2026-04-22 12:57 EDT`), never UTC.
- No em dashes, no emojis/icons unless the developer asks.
- Use `uv run python3` (not plain `python3`) to run scripts.
- One terminal command at a time. Wait for completion before sending the next.
- Log generously. Save full agent transcripts and output, not partial.

## "Task" disambiguation

Three unrelated meanings of "task" in this repo:

- **User tasks** - the user's personal and work to-dos (email follow-ups, errands, projects). Live in `Taskflow/` envelopes. Read `.agents/To Do Task.md`.
- **Triggered tasks** - automated agents that run on cron or file-watch (exercise-sync, todo-pull, dashboard-update). Defined in `Agents/`. Read `.agents/triggered-tasks.md`.
- **Coding TODOs / backlog** - development work items for this repo's infrastructure. Tracked in skill docs and changelogs, not in Taskflow.

## Structure

- `Agents/` - triggered task definitions, handlers, logs
- `Exercise/` - exercise program. Read `.agents/exercise.md` first.
- `Taskflow/` - user task management. Replaced the old `To Do/` system. Envelopes (work/, personal/) with Active/, Inbox/, Monitoring/.
- `To Do/` - legacy; superseded by Taskflow. Still receives Microsoft To Do sync but Taskflow is the primary interface.
- Other folders (`AI and Prompts/`, `Coding/`, `Home/`, `Investigations/`, `Notes/`, `People/`, `Work/`) are notes - self-explanatory.
