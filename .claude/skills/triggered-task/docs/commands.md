# Command Reference

Detailed reference for triggered-task commands. SKILL.md has the dispatch
table and invocation syntax. Load only the section you need.

## Contents

- [install -- Installation Instructions](#install----installation-instructions)
- [prereqs -- Check Environment Readiness](#prereqs----check-environment-readiness)
- [smoketest -- End-to-End Validation](#smoketest----end-to-end-validation)
- [create -- Create a Task](#create----create-a-task)
- [release -- Audit, Assemble, and Publish](#release----audit-assemble-and-publish)
- [Logging internals](#logging-internals)

## `install` -- Installation Instructions

Run `prereqs` first to identify what is missing, then install accordingly.

### Required tools

1. **uv** -- Python package/environment manager. All scripts run via `uv run`.
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   After install, restart your shell or run `source ~/.local/bin/env`.

2. **cron** -- Scheduler for cron-triggered tasks.
   ```bash
   sudo apt install cron
   ```
   Verify with `crontab -l` (should not error).

3. **inotifywait** -- File-watcher for watch-triggered tasks.
   ```bash
   sudo apt install inotify-tools
   ```

4. **agency copilot** -- Default agent runtime. Requires Node.js + npm first.

   a. Install Node.js if missing:
   ```bash
   nvm install --lts
   ```

   b. Install the GitHub Copilot CLI:
   ```bash
   npm i -g @github/copilot
   ```

   c. Install the Agency CLI:
   ```bash
   curl -sSfL https://aka.ms/InstallTool.sh | sh -s agency && exec $SHELL -l
   ```

   d. Complete interactive auth (one-time):
   ```bash
   agency copilot -p "say hello"
   ```

### Optional tools

- **claude CLI**: Only needed for `claude` or `agency claude` agent tasks.
  ```bash
  npm i -g @anthropic-ai/claude-code
  ```
- **jq**: Needed by some processors for JSON parsing.
  ```bash
  sudo apt install jq
  ```

### Workflow

1. Run `prereqs` to see what is missing.
2. Install missing tools using the commands above.
3. Run `prereqs` again to confirm everything passes.
4. Run `smoketest` to validate end-to-end.

---

## `prereqs` -- Check Environment Readiness

Verify that the environment is ready to run triggered tasks. Run each check
below and report the results. If any check fails, explain how to fix it.

### Checks to perform

1. **Platform**: Confirm running on Linux under WSL, not native Windows.
   ```bash
   grep -qi microsoft /proc/version 2>/dev/null
   ```
   Fail message: "Not running in WSL. Switch to a WSL terminal."

2. **python3**: Required -- all scripts are Python 3.12+.
   ```bash
   command -v python3
   ```
   Fix: `sudo apt install python3`

3. **uv**: Required -- Python package/environment manager for processor dispatch.
   ```bash
   command -v uv
   ```
   Fix: `curl -LsSf https://astral.sh/uv/install.sh | sh`

4. **node / npm**: Required for installing agent CLIs and MCP servers.
   ```bash
   command -v node && command -v npm
   ```
   Fix: install Node.js (e.g. `nvm install --lts`)

5. **claude CLI**: Required for `claude` and `agency claude` agent tasks.
   ```bash
   command -v claude
   ```
   Fix: `npm i -g @anthropic-ai/claude-code`

6. **copilot CLI**: Required for `copilot` and `agency copilot` agent tasks (optional if only using claude agents).
   ```bash
   command -v copilot
   ```
   Fix: `npm i -g @github/copilot`

7. **agency CLI**: Required for `agency claude` and `agency copilot` agent tasks (optional if only using bare agents).
   ```bash
   command -v agency
   ```
   Fix (Linux/WSL): `curl -sSfL https://aka.ms/InstallTool.sh | sh -s agency && exec $SHELL -l`
   Fix (Windows): `iex "& { $(irm aka.ms/InstallTool.ps1)} agency"`
   After install, run `agency copilot -p "say hello"` once to complete interactive EntraID auth.

8. **crontab**: Required for cron-scheduled tasks.
   ```bash
   command -v crontab
   ```
   Fix: `sudo apt install cron`

9. **jq**: Required by processors for JSON parsing.
   ```bash
   command -v jq
   ```
   Fix: `sudo apt install jq`

10. **inotifywait**: Required for file-watcher tasks (optional for cron-only).
    ```bash
    command -v inotifywait
    ```
    Fix: `sudo apt install inotify-tools`

11. **Processor directory**: `Agents/handlers/` exists.
    ```bash
    test -d Agents/handlers
    ```

12. **Tasks directory**: `Agents/` exists.
    ```bash
    test -d Agents
    ```

13. **desired-tasks.json watchers populated**: Tasks with `watchPath` frontmatter
    should be listed in `Agents/data/desired-tasks.json` watchers array.
    If the list is empty but tasks define `watchPath`, the health check
    cannot auto-restart dead watchers.
    ```bash
    python3 -c "import json; d=json.load(open('Agents/data/desired-tasks.json')); w=d.get('watchers',[]); print(f'{len(w)} watchers registered') if w else print('WARNING: no watchers in desired-tasks.json')"
    ```
    Fix: `uv run .claude/skills/triggered-task/scripts/activate.py --all`

14. **File watchers running**: At least one `inotifywait` process should be
    alive if tasks with `watchPath` are configured.
    ```bash
    pgrep -c -f "inotifywait.*repos/life" || echo "no watchers running"
    ```
    Fix: `uv run .claude/skills/triggered-task/scripts/activate.py --all`

### Output format

```
Prerequisites for triggered-task:
  [PASS] Platform: WSL (Linux 6.6.x-microsoft-standard-WSL2)
  [PASS] python3: /usr/bin/python3 (3.12.3)
  [PASS] uv: /home/user/.local/bin/uv
  [PASS] node: /usr/bin/node (v20.x)
  [PASS] claude CLI: /home/user/.local/bin/claude
  [WARN] copilot CLI: not found (needed for copilot agent tasks)
  [WARN] agency CLI: not found (needed for agency agent tasks)
  [PASS] crontab: /usr/bin/crontab
  [PASS] jq: /usr/bin/jq
  [PASS] inotifywait: /usr/bin/inotifywait
  [PASS] Processors: Agents/handlers/
  [PASS] Tasks: Agents/ (3 task prompts)
  [PASS] desired-tasks.json: 11 watchers registered
  [PASS] File watchers: 11 inotifywait processes running

  Ready to go (2 warnings).
```

---

## `smoketest` -- End-to-End Validation

Validates the full chain: create -> frontmatter -> status -> agent CLI -> JSON
output -> post-processor -> file write -> watcher detect -> agent CLI -> confirm -> teardown.

**Requires unsandboxed execution.** The smoketest uses inotifywait (kernel
inotify) and agent CLI network calls, both blocked in the VS Code sandbox.
Always run with `requestUnsandboxedExecution: true` or in a regular terminal.

Before running, verify readiness with `prereqs`. If the user asks to run the
smoketest directly, run `prereqs` first and stop if any required check fails.

```bash
uv run .claude/skills/triggered-task/scripts/smoketest.py
```

The script will:
1. Create a test cron task (`smoketest-cron`) with frontmatter
2. Create a test watcher task (`smoketest-watcher`) with frontmatter
3. Verify `status.py --json` reads frontmatter correctly for both tasks
4. Start the watcher
5. Run the cron task via agent CLI, validate JSON + post-processor output
6. Verify watcher detects the file change, run watcher agent
7. Confirm all outputs (files, logs, status table)
8. Tear down both test tasks, verify cleanup

---

## `create` -- Create a Task

Parse the user's description to extract:
- **name** -- slugified from the task title (e.g. "AI News Digest" -> `ai-news-digest`)
- **agent** -- one of: `claude`, `copilot`, `agency claude`, `agency copilot` (default: `agency copilot`)
- **mode** -- `plan` (read-only, preferred) or `write` (default: `plan`)
- **post-processor** -- post-processor script name, or omit for log-only (default: `write-files.sh` if using JSON output)
- **mcps** -- MCP server names for agency agents
- **schedule** -- cron expression (for cron type)
- **watchPath** -- repo-relative directory to monitor (for watcher type)
- **outputDirectory** -- where the post-processor should write files (used in prompt generation)

### Steps

1. Ensure logs directory exists:
   ```bash
   mkdir -p "Agents/logs"
   ```

2. Generate `Agents/<name>.md` with YAML frontmatter and task instructions.
   Include frontmatter with all config fields. If using a post-processor, include the
   JSON output section in the prompt body:
   ```markdown
   ---
   agent: agency copilot
   mode: plan
   post-processor: write-files.sh
   schedule: "0 9 * * *"
   ---

   # Task Title

   Instructions for the agent...

   ## Output
   Respond with ONLY a JSON object, no other text:
   {
     "files": [
       { "path": "<outputDirectory>/filename.md", "content": "file content" }
     ],
     "summary": "one-line description of what was produced"
   }
   ```

3. Show the user what was created and suggest next steps:
   ```
   Created task "<name>":
     prompt:   Agents/<name>.md
     post-processor:  Agents/handlers/<post-processor>
     schedule: <schedule or watchPath>
     agent:    <agent> (<mode> mode)

   Edit the prompt, then: /triggered-task run <name>
   ```

---

## `release` -- Audit, Assemble, and Publish

Package the triggered-task system as a standalone, shareable release.

### Steps

1. **Pre-release audit** -- scan for personal data, secrets, and portability issues:
   ```bash
   uv run .claude/skills/triggered-task/scripts/pre-release-audit.py
   ```
   All checks must pass before proceeding. Use `--fix-paths` to auto-correct
   hardcoded paths.

2. **Assemble** -- copy release-included files into a clean directory:
   ```bash
   bash .claude/skills/triggered-task/scripts/assemble-release.sh [output-dir]
   ```
   Default output: `/tmp/triggered-task-release`.

3. **Verify** -- run the smoketest from the assembled directory:
   ```bash
   cd /tmp/triggered-task-release
   uv run .claude/skills/triggered-task/scripts/smoketest.py --agent claude
   ```

4. **Publish** -- push to the release repository and tag.

Full details: [release-process.md](release-process.md)

---

## Direct shell usage

Quick reference for running scripts directly (without the skill dispatcher):

```bash
S=.claude/skills/triggered-task/scripts

# Lifecycle
uv run $S/run.py  --name my-task         # test it
uv run $S/activate.py  --name my-task         # start cron or watcher
uv run $S/teardown.py  --name my-task --keep  # stop only
uv run $S/teardown.py  --name my-task         # stop + delete

# Info
uv run $S/status.py                            # human-readable table
uv run $S/status.py --json                     # all tasks as JSON
uv run $S/status.py --json my-task             # single task as JSON

# Smoketest (validates full chain for each agent)
uv run $S/smoketest.py --agent claude
uv run $S/smoketest.py --agent copilot
uv run $S/smoketest.py --agent "agency copilot"
```

---

## CLI flags and cron reference

### Flag mapping (implemented in `headless.py`)

| Flag | `claude` | `copilot` |
|------|----------|-----------|
| Headless prompt | `-p "..."` | `-p "..."` |
| Plan mode | `--permission-mode plan` | `--deny-tool shell --deny-tool write --autopilot` by default; task `allow-tools` can opt specific tools back in |
| Write mode | `--dangerously-skip-permissions` | `--allow-all-tools --autopilot` |
| No questions | *(implicit with -p)* | `--no-ask-user` |
| Custom instructions | repo defaults | `--no-custom-instructions` in triggered-task runtime |
| MCP injection | N/A | N/A (agency: `--mcp <name>`) |

Agency variants (`agency claude`, `agency copilot`) use the same flags as their
base CLI, plus `--mcp` for M365 servers.

Copilot-based triggered tasks pass the full task body via `-p` and disable
custom instruction loading with `--no-custom-instructions`. This keeps
background runs self-contained and avoids extra AGENTS/task-file reads that can
trigger Copilot conversation-history mismatches during unattended execution.

### Cron line examples

```bash
# plan + handler (logging is handled internally by run.py via JSONL)
0 0 */3 * * cd /repo && /home/you/.local/bin/uv run --script .claude/skills/triggered-task/scripts/run.py --name ai-news-digest --quiet 2>&1

# plan + log only
0 9 * * * cd /repo && /home/you/.local/bin/uv run --script .claude/skills/triggered-task/scripts/run.py --name my-task --quiet 2>&1
```

`activate.py` writes the absolute path to `uv` into each cron line so the
entry survives even if the crontab `PATH=` prefix is stripped. The
`--script` flag tells `uv` to honor the PEP 723 metadata block in
`run.py` and resolve PyYAML automatically.

### Common cron expressions

| Schedule | Expression |
|----------|-----------|
| Every hour | `0 * * * *` |
| Every 5 minutes | `*/5 * * * *` |
| Daily at 9 AM | `0 9 * * *` |
| Every 3 days | `0 0 */3 * *` |
| Every Monday at 8 AM | `0 8 * * 1` |
| 1st of month | `0 0 1 * *` |

---

## Script architecture

All Python entrypoints import `headless.py` for agent invocation. No script
hard-codes CLI flags -- all flag mapping is in one place.

### headless.py exports

| Function | Purpose |
|----------|---------|
| `parse_frontmatter(prompt_path)` | Extract YAML frontmatter into a typed `TaskConfig` |
| `TaskConfig.task_type` | Returns "cron" or "watcher" from frontmatter |
| `TaskConfig.transcript_log` | Path to transcript file: `Agents/logs/<name>-transcript.md` |
| `list_tasks` | Lists all task names (`.md` files under `Agents/`, excluding `_index_.md`) |
| `build_agent_flags(agent, mode)` | CLI flags for agent+mode |
| `build_agent_command(config, prompt)` | Full command arguments (incl. `--share` when transcript enabled) |
| `resolve_task_command(name)` | Full resolved command from prompt frontmatter |
| `headless_agent(config, prompt)` | Run headlessly, return `AgentResult` (incl. `transcript_path`) |

### Log schema

Each JSONL log entry is a flat JSON object. Key fields:

| Field | Type | When |
|-------|------|------|
| `ts` | string | Always -- ISO 8601 UTC timestamp |
| `task` | string | Always -- task name |
| `phase` | string | Always -- `start`, `pre-processor`, `agent`, `post-processor`, `done`, `output` |
| `level` | string | Always -- `info`, `warning`, `error` |
| `status` | string | On `phase: done` -- `ok`, `error`, `skipped` |
| `message` | string | Most entries -- human-readable description |
| `output` | string | On agent/pre-processor phases -- up to `MAX_LOG_FIELD_LENGTH` chars |
| `error_category` | string | On errors -- `timeout`, `cli_crash`, `output_parse_error`, `pre_processor_crash`, `handler_crash`, `agent_error` |
| `traceback` | string \| null | On errors with stderr -- last Python traceback extracted from stderr, or null |
| `transcript_path` | string | On agent phase -- path to transcript file (when available) |
| `model` | string | On agent phase -- model used |
| `tokens_in` | string | On agent phase -- input token count |
| `tokens_out` | string | On agent phase -- output token count |
| `tokens_cached` | string | On agent phase -- cached token count |
| `premium_requests` | string | On agent phase -- copilot premium request count |
| `cost_usd` | string | On agent phase -- claude cost in USD |
| `structured_output` | object \| null | On agent phase -- parsed JSON when `extract_first_json_value()` succeeds |
| `duration_s` | float | On `phase: done` -- total run duration in seconds |
| `trigger` | string | On `phase: start` -- `cron`, `file-change`, `manual` |
| `log_schema` | int | Always -- schema version (current: 3) |

### Schema evolution

Every log entry carries a `log_schema` integer. When the schema changes,
bump the version, backpatch all existing data, and record the migration
here. The goal is **fix the data, not the queries** -- DuckDB infers
types per-file, so inconsistent types across files cause UNION crashes.

**Procedure for schema changes:**

1. **Bump `log_schema`** in `log_event()` (`headless.py`).
2. **Write a backpatch script** (see `Agents/scripts/backpatch-logs.py`
   for the template). The script must handle both live JSONL and
   archived Parquet. Run with `--dry-run` first, then `--apply`.
3. **Record the migration** in the version table below.
4. **Update the schema table** above and the changelog.
5. **Remove any query workarounds** (COALESCE, VARCHAR casts, etc.)
   that the old data required. Per AGENTS.md: no backward-compat shims.

**Version history:**

| Version | Date | Migration |
|---------|------|-----------|
| 1 | (implicit) | Original schema. No `log_schema` field. |
| 2 | 2026-04-26 | Added `task` to all entries. Normalized `ts` to ISO 8601 with `T` separator and `Z` suffix. Coerced `duration_s` to float, `changed_files` to list, `skip` to bool. Expanded token abbreviations (`1.1k` -> `1100`). Dropped null-valued keys. Added `log_schema: 2`. |
| 3 | 2026-05-09 | Repaired type drift that was crashing `qlog.py --all` with a DuckDB `BinderException`. Parsed stringified arrays in `changed_files` / `skipped_files` (Python-repr `"['a','b']"` and naive `str(list)` `"[a, b]"`) into real lists so the unioned column resolves to `VARCHAR[]` (required by `list_transform`). Coerced phase durations (`pre_processor_s`, `agent_s`, `post_processor_s`, `timeout_s`) to float and exercise/parse-tracking counts (`exercise_days`, `rest_travel_days`, `total_days`, `line_count`, `parsed_count`, `unparsed_count`) to int. `log_schema` itself is now always written as `int`. |

**Backpatch template** -- `Agents/scripts/backpatch-logs.py`:

```bash
# Dry-run (shows what would change, writes nothing)
uv run Agents/scripts/backpatch-logs.py

# Apply changes to all JSONL + Parquet files
uv run Agents/scripts/backpatch-logs.py --apply
```

The script reads every JSONL and Parquet file, normalizes each entry,
and rewrites in place. It is idempotent -- running it twice is safe.
Always verify with `qlog.py --errors --all` after applying.

---

## Logging internals

All JSONL logging goes through `log_event()` in `headless.py`. Key
behaviours:

- **Field validation** -- string fields > 20,000 chars are truncated;
  `_truncated: true` is added. Non-serialisable values fall back to
  `str()`; if that fails, a `phase: log_event` error entry is written.
  Caller-supplied `ts` is silently dropped (always generated).
- **`task` field** -- `run.py` uses `tlog()`/`tlog_start()`/`tlog_end()`
  wrappers that inject `task=config.name` into every per-task log entry.
  Entries are self-describing.
- **`structured_output`** -- when the agent produces parseable JSON,
  `normalize_agent_output()` carries the parsed object through
  `AgentResult.structured_output` and it's written to the JSONL log.
  No task definition changes needed.
- **`error_category`** -- all error paths (agent, handler, pre-processor)
  include an `error_category` field: `timeout`, `cli_crash`,
  `output_parse_error`, `pre_processor_crash`, `handler_crash`,
  `agent_error`.
- **`traceback`** -- error entries with stderr include the last Python
  traceback in a dedicated field via `_extract_traceback()`.

Full schema and querying docs: [approach.md section 4](approach.md#4-logging).
