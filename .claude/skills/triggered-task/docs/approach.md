# Triggered Agent Tasks

> Schedule agent tasks on a cron or file-watcher, manage them with `/triggered-task`.  
> Supports `claude`, `copilot`, `agency claude`, `agency copilot`.  
> **Platform: Linux (Ubuntu on WSL)** | Last updated: May 2026

---

## 1. What It Looks Like

The skill works from any agent CLI. Claude Code uses `/triggered-task` as a
slash command; Copilot and Agency use natural conversation via AGENTS.md.

```
/triggered-task create "AI News Digest" that every 3 days uses claude
in plan mode with a handler that writes output to "AI and Prompts/AI News"
```

```
/triggered-task run ai-news-digest
/triggered-task start ai-news-digest
/triggered-task status
/triggered-task stop ai-news-digest
/triggered-task teardown ai-news-digest
```

### Commands

| Command | What it does |
|---------|-------------|
| `create <desc>` | Parse description, create prompt with frontmatter + handler |
| `run <name>` | Execute once, show output |
| `start <name>` | Activate cron or watcher (auto-detects type) |
| `stop <name>` | Deactivate, keep config |
| `teardown <name>` | Stop + delete task (logs preserved) |
| `status` | List all tasks and state |
| `logs <name>` | Show recent output |
| `prereqs` | Check environment readiness |
| `smoketest` | End-to-end validation |
| `release` | Audit, assemble, verify, publish portable release |

### Lifecycle

```
create → run (test) → start (activate) → stop → teardown
```

### Example: status output

```
NAME              TYPE     SCHEDULE      AGENT            MODE  STATE
ai-news-digest    cron     0 0 */3 * *   claude           plan  active
exercise-review   watcher  Exercise/     copilot          plan  active (pid 12345)
weekly-summary    cron     0 8 * * 0     agency copilot   plan  stopped
```

---

## 2. How It Works

### Architecture

```
Agents/                              # all runtime artifacts
├── ai-news-digest.md                # one file per task; source of truth config
├── handlers/                        # handlers co-located with tasks
│   ├── write-files.sh               # generic: JSON files[] → disk
│   └── ms-todo-to-md.sh             # task-specific: To Do JSON → .md files
├── logs/                            # JSONL log files (one per task + system log)
│   ├── triggered-tasks.log          # system-wide: all task activations/completions
│   ├── todo-sync.log             # per-task: detailed execution log
│   ├── todo-index.log
│   └── archive/                     # monthly Parquet archives (auto-rotated weekly)
│       ├── triggered-tasks/
│       │   └── 2026-04.parquet
│       └── todo-push/
│           └── 2026-04.parquet

.claude/skills/triggered-task/       # skill definition
├── SKILL.md
├── docs/
│   └── approach.md
└── scripts/
    ├── headless.py              # shared helper (env, flags, frontmatter parsing)
    ├── activate.py              # start cron or watcher (auto-detects type)
    ├── run.py              # execute task once with verbose output
    ├── logs.py                 # read system/task logs as table or JSON
    ├── teardown.py              # stop + remove a task
    ├── status.py                # list all tasks + state (--json for structured output)
    └── smoketest.py             # end-to-end validation (all agents)
```

### Prompt frontmatter (source of truth)

Each prompt file contains YAML frontmatter that defines **how** the task runs.
This is the authoritative configuration — there is no manifest file.

```yaml
---
agent: agency copilot          # who runs it (default: agency copilot)
mode: plan                     # plan (read-only) or write (default: plan)
model: claude-haiku-4.5        # optional model override
handler: ms-todo-to-md.sh      # handler script name, or omit for log-only
env:                           # optional env vars for the agent process
  EXAMPLE_KEY: example-value
mcps:                          # MCP servers (agency agents only)
  - softeria
schedule: "0 * * * *"          # cron expression (cron tasks)
# watchPath: Exercise/         # directory to monitor (watcher tasks)
---
```

#### Frontmatter fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `agent` | no | `agency copilot` | `claude`, `copilot`, `agency claude`, `agency copilot`, `none` |
| `mode` | no | `plan` | `plan` (read-only) or `write` |
| `model` | no | *(agent default)* | Optional model override passed to the agent CLI |
| `allow-tools` | no | *(none)* | Copilot-only task-level tool allow-list; mainly useful in `plan` mode to opt specific tools like `shell` back in |
| `handler` | no | *(log-only)* | Post-processor script name (bare name resolves to `<task-dir>/handlers/`; path with `/` is repo-relative) |
| `pre-processor` | no | *(none)* | Pre-processor script; runs before the agent and can replace/skip the prompt |
| `post-processor` | no | *(none)* | Alias for `handler` (takes precedence when both are set) |
| `env` | no | *(none)* | Map of env vars passed to the agent process |
| `mcps` | no | *(none)* | List of MCP server names (agency agents only) |
| `schedule` | one of | — | Cron expression (makes it a cron task) |
| `watchPath` | these | — | Repo-relative path(s) to monitor (string or list; makes it a watcher task) |
| `watchIgnore` | no | *(none)* | Glob patterns to exclude from watcher events (string or list) |
| `debounce` | no | *(none)* | Seconds to delay watcher dispatch via systemd timer (Layer 2 debounce) |
| `timeout` | no | `120` | Max seconds for agent execution |
| `transcript` | no | `true` | Capture full session transcript (copilot: `--share`). Set `false` to disable for noisy/stable tasks |

**Defaults:** If `agent` is omitted, defaults to `agency copilot`. If `mode` is
omitted, defaults to `plan`. A task with `schedule` is a cron task; a task with
`watchPath` is a watcher task.

### Querying task state: `status.py`

There is no manifest file. Task configuration lives in prompt frontmatter and
runtime state is computed on demand from crontab and process lists.

```bash
uv run .claude/skills/triggered-task/scripts/status.py
uv run .claude/skills/triggered-task/scripts/status.py --json
uv run .claude/skills/triggered-task/scripts/status.py --json <name>
```

Infrastructure scripts (`activate.py`, `run.py`, `status.py`, `teardown.py`,
`logs.py`, `smoketest.py`, `headless.py`) declare their dependencies (PyYAML)
inline via PEP 723 `# /// script` blocks and are invoked through `uv run`.
This keeps the skill portable: `uv` resolves the environment on demand, no
shared `.venv` or system `pip install` is required. Handlers use the same
pattern: `build_handler_command()` in headless.py injects base packages
via `uv run --with`, and handlers needing extras declare them inline
via PEP 723 `# /// script` blocks.

The JSON output includes all frontmatter fields plus computed state:

```json
{
  "tasks": [
    {
      "name": "todo-sync",
      "type": "cron",
      "agent": "agency copilot",
      "mode": "plan",
      "promptPath": "Agents/todo-sync.md",
      "state": "stopped",
      "handler": "Agents/handlers/ms-todo-to-md.sh",
      "schedule": "0 * * * *",
      "mcps": ["softeria"]
    }
  ]
}
```

| Field | Source |
|---|---|
| name | Stem of `Agents/<name>.md` |
| agent, mode, model, handler, mcps, env | Prompt frontmatter |
| type, schedule, watchPath | Frontmatter (`schedule:` → cron, `watchPath:` → watcher) |
| promptPath | Convention: `Agents/<name>.md` |
| state | `crontab -l` for cron, process list inspection for watchers |

Watcher runs ignore changes under hidden `.*` directories, `__pycache__/`, and
`Agents/logs/` so repo-root watchers do not loop on Git metadata or their own
runtime log writes.

### Execution modes

The agent is invoked headless (`-p`) and its output goes one of three ways:

| Mode | How | When |
|------|-----|------|
| **Plan + handler** | Agent runs read-only, outputs JSON, handler acts on it | Preferred — safe and deterministic |
| **Plan + log** | Agent runs read-only, output written to log file | Analysis/reporting tasks |
| **Direct write** | Agent has write access, modifies files directly | When you trust the prompt fully |
| **Handler-only** | No agent — handler runs directly (`agent: none`) | When no LLM is needed |

**Handler-only** (`agent: none`) skips the LLM entirely and runs the handler
script directly with no stdin. Use this when the task is purely mechanical
(e.g. indexing files, syncing state) and doesn't need reasoning. Works with
both `schedule:` (cron) and `watchPath:` (watcher) triggers. A `handler:` field
is required when `agent: none`.

**Plan + handler** is preferred: the agent thinks, the script acts. Prompts
include a JSON output section; handlers process the JSON. The generic handler
(`write-files.sh`) creates files from JSON. Write task-specific handlers for
custom processing (e.g. `ms-todo-to-md.sh` transforms To Do API data into
Obsidian notes).

### Agent support

1. **Agent as task executor**: The `agent` field in the prompt frontmatter
   determines which CLI runs the task headlessly (`claude`, `copilot`,
   `agency copilot`, etc.). Default is `agency copilot`.

2. **Agent as skill runner**: Copilot reads SKILL.md via AGENTS.md and invokes
   the same scripts conversationally — no slash command needed.

### Agent-specific behavior

| | Claude | Copilot | Agency Claude | Agency Copilot |
|--|--------|---------|---------------|----------------|
| Headless capture | `$(...)` works | Needs `script -qc` (writes to /dev/tty) | `$(...)` works | `$(...)` works (agency handles tty internally) |
| System prompt | `--append-system-prompt` | Not available | `--append-system-prompt` | Not available |
| Output format flag | `--output-format json` | N/A | `--output-format json` | N/A |
| JSON output format | JSON envelope with `result` + `usage` | Raw JSON | JSON envelope (same as claude) | Often markdown-fenced (` ```json `) |
| JSON reliability | High | Medium (needs checklist prompt) | High | High |
| Auth | API key | API key | API key + EntraID | EntraID (one-time interactive) |
| MCP disable flags | N/A | `--disable-mcp-server`, `--no-default-mcps` | N/A | `--disable-mcp-server`, `--no-default-mcps` |

### Token usage logging

Every task run logs token usage on the `phase: agent` log entry. The source
of usage data varies by agent:

| Agent | Usage source | Fields logged |
|-------|-------------|---------------|
| `claude` | `--output-format json` response body | `model`, `tokens_in`, `tokens_out`, `tokens_cached`, `cost_usd` |
| `agency claude` | Same as claude (JSON envelope) | `model`, `tokens_in`, `tokens_out`, `tokens_cached`, `cost_usd` |
| `copilot` | stderr summary line | `tokens_in`, `tokens_out`, `tokens_cached`, `premium_requests` |
| `agency copilot` | stderr summary line | `tokens_in`, `tokens_out`, `tokens_cached`, `premium_requests` |

**Copilot/agency copilot stderr formats** (both supported by `parse_usage_stats()`):

```
# New format (agency v2026.4.9+, copilot CLI):
Requests  3 Premium (15s)
Tokens    ↑ 55.8k • ↓ 805 • 27.8k (cached)

# Old format (agency <v2026.4.9):
Total usage est:        3 Premium requests
claude-opus-4.6          162.3k in, 971 out, 0 cached
```

**Claude JSON envelope** (parsed by `parse_claude_json_output()`):

```json
{
  "result": "<agent output text>",
  "total_cost_usd": 0.0308,
  "usage": {"input_tokens": 3, "output_tokens": 174, "cache_read_input_tokens": 12380},
  "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 3, "outputTokens": 174}}
}
```

The `result` field is unwrapped to become the agent output; usage fields are
extracted and logged alongside the output.

### Smoketest

`/triggered-task smoketest` validates the full chain end-to-end:
create → frontmatter → status → agent CLI → JSON output → handler → file write →
watcher detect → agent CLI → teardown

Supports all agents via `--agent`. Stops and cleans up on failure.

---

## 3. Prerequisites

| Tool | Install | Purpose |
|------|---------|---------|
| `python3` (≥ 3.12) | `sudo apt install python3` | All scripts are Python |
| `uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | Python package/environment manager |
| `node` / `npm` | `nvm install --lts` | Installing agent CLIs and MCP servers |
| Claude Code | `npm i -g @anthropic-ai/claude-code` | Agent CLI for `claude` / `agency claude` |
| Copilot CLI | `npm i -g @github/copilot` | Agent CLI for `copilot` / `agency copilot` |
| Agency | See [agency cli.md](reference/agency%20cli.md) | EntraID auth + M365 MCPs |
| `crontab` | `sudo apt install cron` | Cron-scheduled tasks |
| `jq` | `sudo apt install jq` | JSON parsing in handlers |
| `inotifywait` | `sudo apt install inotify-tools` | File watcher |

### MCP authentication

Tasks using MCP servers (e.g. `@softeria/ms-365-mcp-server`) require a
one-time device code login before headless use:

```bash
MS365_MCP_TOKEN_CACHE_PATH="$HOME/.config/ms365-mcp/.token-cache.json" \
MS365_MCP_SELECTED_ACCOUNT_PATH="$HOME/.config/ms365-mcp/.selected-account.json" \
npx -y @softeria/ms-365-mcp-server --login
```

Follow the browser prompt to authenticate. Token cache is stored at
`~/.config/ms365-mcp/.token-cache.json`.

Use `/triggered-task prereqs` to verify all are installed.

---

## 4. Logging

All logs use JSONL format (one JSON object per line) in `Agents/logs/`.

### Log files

| File | Purpose |
|------|---------|
| `Agents/logs/<name>.log` | Per-task execution log — start, agent output, handler output, done |
| `Agents/logs/triggered-tasks.log` | System-wide summary — every task activation, completion, error |

### JSONL schema

Each line is a JSON object with at minimum `ts` (ISO-8601 UTC) and `phase`:

```json
{"ts":"2026-04-08T13:05:01Z","phase":"start","trigger":"cron","agent":"agency copilot","mode":"plan","handler":"ms-todo-to-md.sh"}
{"ts":"2026-04-08T13:05:15Z","phase":"agent","status":"ok","output":"...","model":"claude-sonnet-4-6","tokens_in":"12.4k","tokens_out":"174","tokens_cached":"12.4k","cost_usd":"0.0308"}
{"ts":"2026-04-08T13:05:16Z","phase":"handler","status":"ok","message":"wrote 1 file, 9 unchanged"}
{"ts":"2026-04-08T13:05:16Z","phase":"done","status":"ok","duration_s":15.0}
```

The `phase: agent` entry includes optional usage fields when available:
`model`, `tokens_in`, `tokens_out`, `tokens_cached`, `premium_requests`
(copilot), `cost_usd` (claude). See "Token usage logging" above.

The system log (`triggered-tasks.log`) adds a `task` field and records
activations, completions, and errors:

```json
{"ts":"2026-04-08T13:05:01Z","task":"todo-sync","phase":"start","trigger":"cron"}
{"ts":"2026-04-08T13:05:16Z","task":"todo-sync","phase":"done","status":"ok","agent":"ok","handler":"ok","duration_s":15.0}
{"ts":"2026-04-08T13:10:01Z","task":"todo-sync","phase":"done","status":"error","message":"agent exited with status 2","duration_s":2.1}
```

### Querying logs

**`qlog.py`** is the primary query tool -- it wraps DuckDB over both live
JSONL and archived Parquet files. Located at
`.claude/skills/triggered-task/scripts/qlog.py` (PEP-723, `uv run`).

```bash
S=.claude/skills/triggered-task/scripts

# All errors across all logs (live + archive)
uv run --script $S/qlog.py --errors --all

# Events for one task in a time window
uv run --script $S/qlog.py --task exercise-state-update --since 2026-04-22T13:00 --until 2026-04-22T13:30

# Slow runs (duration > 30s)
uv run --script $S/qlog.py --slow 30 --since 2026-04-22

# Custom SQL against the `log` view
uv run --script $S/qlog.py --all --sql "SELECT task, COUNT(*) FROM log WHERE status='error' GROUP BY 1 ORDER BY 2 DESC"

# Output formats: table (default), jsonl, csv
uv run --script $S/qlog.py --errors --all --format csv
```

Filters: `--task`, `--since`, `--until`, `--phase`, `--status`, `--trigger`,
`--slow SEC`, `--errors`. All optional, AND'd together.

**`timeline.py`** produces a human-readable merged timeline across all log
files with phase icons (WATCH/START/PREP/AGENT/POST/DONE/FAIL/SKIP/SYSD).
Optionally interleaves systemd journal entries.

```bash
# Timeline for a specific task
uv run --script $S/timeline.py exercise-state-update --since 2026-05-01T12:00

# Content substring filter (matches task name, message, or any field)
uv run --script $S/timeline.py LMCO --systemd --last 30

# All tasks in a window
uv run --script $S/timeline.py --all --since 2026-05-01T16:00
```

**Quick alternatives** (live logs only, no archive):

```bash
# Table view via logs.py
uv run .claude/skills/triggered-task/scripts/logs.py todo-pull -n 20

# Raw jq on live JSONL
jq 'select(.status=="error")' Agents/logs/todo-pull.log
```

### Log rotation (implemented)

The `log-rotate` task (`Agents/log-rotate.md`, cron `0 3 * * 0`) runs
weekly. The `Agents/handlers/log-rotate.py` pre-processor:

1. Reads each `.log` file in `Agents/logs/` and `Exercise/data/log/`
2. Moves rows older than 7 days into monthly Parquet files at
   `Agents/logs/archive/<logname>/YYYY-MM.parquet` (ZSTD compressed)
3. Rewrites the live `.log` with only the last 7 days

Monthly Parquet files are rewritten each rotation (union existing archive
+ new aged-out rows -> fresh file). `qlog.py` reads both live JSONL and
archived Parquet transparently -- no flag needed.

**Archive layout:**

```
Agents/logs/archive/
  triggered-tasks/
    2026-04.parquet
  todo-push/
    2026-04.parquet
  note-index/
    2026-04.parquet
  ...
```

One subdirectory per log name, one Parquet file per month.

### Teardown behavior

`teardown` removes the task file but preserves logs by default. Use
`--delete-logs` to also remove the task's log file.

---

## Related documents

| Document | Content |
|----------|---------|
| [key-learnings.md](key-learnings.md) | Implementation details, gotchas, and patterns discovered during development |\n| [diagnostics.md](diagnostics.md) | Log inventory, diagnostic procedures, common patterns, query recipes |
| [changelog.md](changelog.md) | Reverse-chronological log of infrastructure changes |
| [backlog.md](backlog.md) | Future work items, roughly ordered by value |
| [commands.md](commands.md) | Command reference, CLI flags, cron expressions, script architecture, log schema |
| [reference/copilot-cli-headless-guide.md](reference/copilot-cli-headless-guide.md) | Practical guide to running Copilot CLI headless |
| [reference/wsl-runbook.md](reference/wsl-runbook.md) | WSL operational runbook: debugging, restarting, verifying |
| [reference/cascade-modeling.md](reference/cascade-modeling.md) | Methodology for proving watcher cascades terminate |
| [reference/session-transcript-capture.md](reference/session-transcript-capture.md) | Research on session transcript capture |
| [reference/agency cli.md](reference/agency%20cli.md) | Agency CLI installation and usage |

