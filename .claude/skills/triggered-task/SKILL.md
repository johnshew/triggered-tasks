---
name: triggered-task
description: >-
  Create, test, activate, and tear down scheduled (cron) and file-watch
  triggered agent tasks. Supports claude, copilot, agency claude, agency copilot.
  Triggers: "triggered task", "create a task", "schedule a task", "set up a watcher",
  "triggered-task create", "triggered-task run", "triggered-task status",
  "review task logs", "why did X not pick up", "debug watcher race",
  "trace a pipeline run", "what triggered rebuild".
---

# Triggered Task Management

Each task is a single file at `Agents/<name>.md` (or in an additional task
directory) with YAML frontmatter defining how the task runs. Runtime state
is computed from crontab and process lists.

## Load before acting

| Before doing... | Read first |
|---|---|
| `create` (building a new task) | [docs/commands.md](docs/commands.md) section "create" |
| `install` or `prereqs` | [docs/commands.md](docs/commands.md) -- or just run the script; output is self-documenting |
| `smoketest` | [docs/commands.md](docs/commands.md) section "smoketest" |
| `release` | [docs/release-process.md](docs/release-process.md) |
| Editing any script | [docs/approach.md](docs/approach.md) (architecture) |
| Debugging log issues | [docs/diagnostics.md](docs/diagnostics.md) (log inventory, procedures, patterns, query recipes) |
| Debugging cron/watcher lifecycle | [docs/key-learnings.md](docs/key-learnings.md) |
| Debugging WSL/9P issues | [docs/reference/wsl-runbook.md](docs/reference/wsl-runbook.md) |
| Reviewing implementation history | [docs/changelog.md](docs/changelog.md) |
| Planning future work | [docs/backlog.md](docs/backlog.md) |

If you change behavior that contradicts approach.md, update it in the same
commit. Stale docs are worse than missing ones.

## Task directories

Task prompts live in `Agents/` by default. Additional directories can be
registered in `Agents/data/config.json` under `task_directories`. Names
must be unique across all directories.

Handler and pre/post-processor resolution for bare names (no `/`) is
**relative to the task's own directory**. Use a repo-relative path to
reference handlers from another directory. Logs are always centralized
in `Agents/logs/`.

## Lifecycle

```
create -> run (test) -> start (activate) -> stop -> teardown
```

## Commands

| Pattern | Command | Script |
|---------|---------|--------|
| `create <description>` | Create a new task | *(generates files -- see [docs/commands.md](docs/commands.md))* |
| `run <name>` | Execute once with verbose output | `uv run .claude/skills/triggered-task/scripts/run.py --name "<name>"` |
| `start <name>` | Activate cron/watcher | `uv run .claude/skills/triggered-task/scripts/activate.py --name "<name>"` |
| `stop <name>` | Deactivate cron/watcher | `uv run .claude/skills/triggered-task/scripts/teardown.py --name "<name>"` |
| `status` | List all tasks and state | `uv run .claude/skills/triggered-task/scripts/status.py` |
| `teardown <name>` | Same as stop | `uv run .claude/skills/triggered-task/scripts/teardown.py --name "<name>"` |
| `logs [name]` | Show recent log entries | `uv run .claude/skills/triggered-task/scripts/logs.py [name] [--errors] [--all] [-n 50]` |
| `logs query [name]` | Query logs with DuckDB | `uv run --script .claude/skills/triggered-task/scripts/qlog.py [--task name] [--errors] [--all] [--since T] [--slow N]` |
| `logs timeline [name]` | Correlated event timeline | `uv run --script .claude/skills/triggered-task/scripts/timeline.py [name] [--all] [--since T] [--systemd]` |
| `smoketest` | End-to-end validation | `uv run .claude/skills/triggered-task/scripts/smoketest.py` |
| `prereqs` | Check environment readiness | *(run checks per [docs/commands.md](docs/commands.md))* |
| `install` | Install required tools | *(see [docs/commands.md](docs/commands.md))* |
| `release` | Audit, assemble, publish | *(see [docs/release-process.md](docs/release-process.md))* |

**Smoketest and commands that touch cron/inotifywait require `requestUnsandboxedExecution: true`.**

## Prompt Frontmatter

Each prompt file's YAML frontmatter is the source of truth for task config.

| Field | Default | Description |
|-------|---------|-------------|
| `agent` | `agency copilot` | `claude`, `copilot`, `agency claude`, `agency copilot`, `none` |
| `mode` | `plan` | `plan` (read-only) or `write` |
| `model` | *(agent default)* | Optional model override |
| `pre-processor` | *(none)* | Script in the task's `handlers/` dir; runs before agent |
| `post-processor` | *(log-only)* | Script in the task's `handlers/` dir; runs after agent |
| `env` | *(none)* | Map of env vars passed to the agent process |
| `mcps` | *(none)* | List of MCP server specs |
| `schedule` | -- | Cron expression (at least one of schedule/watchPath required) |
| `watchPath` | -- | Repo-relative directory or list of directories |

Both `schedule` and `watchPath` can be set (type `multi`). Each trigger
fires independently. On `start`/`stop`, both are activated/deactivated.

## Pre-processor pipeline

```
pre-processor -> agent -> post-processor
```

- Pre-processor stdout is appended to the agent prompt as `pre-processor="<output>"`.
- Output `{"skip": true}` to skip the agent call (status `skipped`).
- With `agent: none`, pre-processor output pipes directly to post-processor (deterministic pipeline).
- Watchers ignore `.*`, `__pycache__/`, and `Agents/logs/` to prevent loops.

## Guardrails

- **Do NOT create unit tests** for tasks, processors, or prompts. The
  smoketest validates the runtime. Explicit user permission required
  before adding test files.
- **Do NOT use `git checkout`, `git reset`, or `git stash`** on tracked
  files -- other agents may have uncommitted work.
- Agency agents require a one-time interactive auth before unattended use.
