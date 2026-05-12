# Key Learnings

Implementation details, gotchas, and patterns discovered while building
and operating the triggered-task system. These are historical notes -- for
the current architecture, see [approach.md](approach.md).

## Python implementation

The triggered-task runtime now uses typed Python entrypoints in
`.claude/skills/triggered-task/scripts/`.

## Two-layer debounce architecture

Watcher dispatch has two independent debounce layers that work in series.
Both must be understood together -- they solve different problems.

```
inotify event
  |
  v
Layer 1: Internal batch debounce (1s, activate.py DEBOUNCE_SECS)
  Collects rapid inotify events into a single batch.
  Fires after 1s of quiet.
  |
  v
Content-hash filter (G1)
  Drops unchanged files from the batch.
  |
  v
Layer 2: Systemd timer debounce (N seconds, frontmatter `debounce: N`)
  Reschedules a transient timer on each new batch.
  Fires N+2s after the LAST batch arrived.
  |
  v
Dispatch (run.py --name <task>)
```

**Layer 1 (always active):** The watcher loop uses `select()` on the
inotifywait fd. After the first event arrives, it reads all immediately
available events, then waits up to `DEBOUNCE_SECS` (1.0s) for more.
Events arriving within that window join the same batch. This prevents
rapid saves from spawning multiple dispatches and exists for ALL watcher
tasks regardless of frontmatter settings.

Location: `activate.py`, `watch_loop()`, around line 355.

**Layer 2 (opt-in via frontmatter):** When a task sets `debounce: N`,
the dispatcher does NOT run the task immediately after Layer 1 produces
a batch. Instead it schedules (or reschedules) a `systemd-run --user`
transient timer that fires N+2 seconds in the future. If another batch
arrives before the timer fires, the timer is cancelled and rescheduled.
Changed files accumulate across batches in `_debounce_pending`. When the
timer finally fires, it dispatches run.py with all accumulated files.

Location: `activate.py`, `_debounce_schedule()`, around line 125.

**Fallback when systemd is unavailable:** If `systemd-run --user` fails
(no D-Bus session, container, sandbox), the dispatch is logged as an error
and skipped. There is no fallback to immediate dispatch -- systemd user
services are a hard requirement for the triggered-task system.

**Interaction between layers:**

| Scenario | Layer 1 | Layer 2 | Dispatches |
|----------|---------|---------|-----------|
| Two saves 0.5s apart, no `debounce` key | Coalesces into 1 batch | N/A (immediate) | 1 |
| Two saves 0.5s apart, `debounce: 5` | Coalesces into 1 batch | Schedules timer | 1 (after 7s) |
| Two saves 5s apart, `debounce: 5` | 2 separate batches | Second reschedules timer | 1 (after 12s) |
| Two saves 5s apart, no `debounce` key | 2 separate batches | N/A (immediate each) | 2 |

**Design rule:** A task's post-processor must never write files back into
the task's own watched directory. This creates a self-triggering cascade
that bypasses debounce (new file content passes the content-hash filter).
Write output elsewhere, or use the cascade-modeling approach for
bidirectional flows between separate tasks.

## Watcher startup and daemonization lessons

The April 2026 watcher failure investigation surfaced a few rules that are
easy to miss when starting Python-based file watchers through `activate.py`.

1. **Do not treat every empty read as EOF.** The watcher loop uses
  non-blocking reads on `inotifywait` stdout so it can debounce rapid file
  changes. In that mode, `read()` can return an empty string even when the
  child is still alive. Only treat empty reads as fatal if `process.poll()`
  shows the watcher subprocess has actually exited. Otherwise the daemonized
  watcher can stop itself and look like it "died silently".

2. **Watcher runtime should log lifecycle events directly.** `activate.py`
  should write explicit `phase: watcher` entries for startup and real watcher
  exits via `log_event(...)`. This is the right place for watcher process
  lifecycle because it is infrastructure-level behavior, not task-specific
  business logic.

3. **Handlers should log to stderr, not open log files directly.** Python
  handlers such as `todo-push.py` should print diagnostic messages to stderr.
  The runtime captures stderr and appends it to the task JSONL log. This keeps
  handler logs in one place and avoids every handler inventing its own logging
  scheme.

4. **Table log output is summarized.** `logs.py` shortens long `message` and
  `output` fields for the human-readable table view. When debugging watcher
  startup, handler stderr, or multi-line diagnostics, use `logs.py --json` or
  inspect the raw JSONL directly. Otherwise it can look like the runtime
  "lost" the important part of the message when it was only truncated for
  display.

5. **Pass changed-file context through the runtime environment.** Watcher
  tasks rely on `TRIGGERED_CHANGED_FILE` being set by `run_handler(...)` and
  `run.py`. If a handler is meant to react to a specific changed file, the
  changed path belongs in environment propagation rather than ad hoc argv
  conventions.

6. **Ignore watcher-generated noise to prevent loops.** Repo-root and broad
  directory watchers should continue to ignore hidden directories,
  `__pycache__/`, `Agents/logs/`, and generated files like `_index_.md`.
  Otherwise watchers can trigger on their own metadata churn and create noisy
  or self-sustaining loops.

7. **Watch directories, not files -- atomic saves replace inodes.** When
  `inotifywait` watches a single file and an editor performs an atomic save
  (write to temp file, then `rename()` over the target), the original inode
  is replaced. `inotifywait` continues watching the old (deleted) inode and
  silently stops receiving events -- no error, the process looks healthy but
  is deaf. `activate.py` now watches the **parent directory** with both
  `close_write` and `moved_to` events, then filters to the target filename.
  This catches both direct writes (Obsidian, shell redirects, Python
  `Path.write_text()`) and atomic saves (VS Code, Copilot CLI, `git pull`).

8. **Aggressive log truncation hides tracebacks.** Earlier versions truncated
  stderr to 200 chars in error log entries. This made Python tracebacks
  completely invisible -- the important diagnostic (exception type, message,
  stack) was always at the *end* of stderr, beyond the truncation point.
  Fix: raise `MAX_LOG_FIELD_LENGTH` to 20,000 chars and extract the last
  Python traceback into a dedicated `traceback` JSONL field. `qlog.py --errors`
  now surfaces tracebacks automatically.

9. **Classify errors at the boundary, not in the catch-all.** Pre-processor
  and handler failures were all lumped into `agent_error`. Adding
  `pre_processor_crash` and `handler_crash` categories lets self-healing
  agents route to the correct fix without parsing the error message.

## Content-hash cascade guard (implemented)

File-watch tasks that write back into their own watch directory create
self-triggering cascades: a task writes output files, inotifywait fires
again, the task reruns on identical content, writes the same output, and
the cycle repeats until something time-limited breaks it. This is the
single biggest source of wasted agent runs and token spend.

**Design:** The watcher dispatcher (`activate.py`) SHA-256 hashes each
changed file after debouncing and compares against a per-task cache
(`Agents/data/<task>-watch-hashes.json`). Files whose content is
unchanged since the last dispatch are **individually dropped** from the
batch. If no files survive filtering, the entire dispatch is skipped.

**Cascade window (120 s).** Hash filtering only applies within 120
seconds of the last dispatch. Outside that window, every event dispatches
unconditionally -- including content-identical `touch` or mtime-only
bumps. This is critical because operators routinely use `touch` to force
a re-run. The cascade window catches self-writes (which happen within
seconds) while preserving the `touch`-to-trigger workflow.

**Key decisions and lessons:**

1. **Filter per-file, not all-or-nothing.** A batch may contain a mix of
   genuinely changed and cascade-echoed files. Drop only the unchanged
   ones; dispatch the rest. All-or-nothing would swallow real edits that
   arrive in the same debounce window as cascade noise.

2. **Guard at the dispatcher, not in each handler.** Putting the hash
   check in `activate.py` gives every watcher task cascade protection
   for free, with zero handler changes. Handler-specific hash logic
   (like exercise-state-prep.py's Tracking.md hash) becomes a
   belt-and-suspenders redundancy -- fine to keep for defense in depth
   but not required for correctness.

3. **Cache includes a dispatch timestamp.** The JSON cache stores
   `_dispatched_at` alongside the file hashes. The dispatcher reads this
   to decide if the cascade window is active. This avoids any dependency
   on file mtimes, which are unreliable across save modes and editors.

4. **Log both filtered and dispatched files with hash prefixes.** Every
   skip and dispatch is logged with 12-char hash prefixes so cascade
   behavior is visible in `qlog.py` without inspecting file contents.
   The task log gets the per-file filter details; the system log gets
   the batch summary.

5. **Don't cache hashes for unreadable files.** Deleted or locked files
   that can't be read are passed through to the dispatch batch without
   hash comparison. The task's own logic should handle missing files.

6. **Multi-watcher cascade analysis.** When multiple watchers share a
   directory (e.g. exercise-state-update watching `Exercise/` and
   exercise-sync-checkboxes watching `Recommendations.md`), the cascade
   guard at the dispatcher (G1) is necessary but not sufficient. Each
   watcher maintains independent caches. Cross-watcher cascades (sync
   writes T -> exercise fires) require task-specific guards (pre-proc
   mtime G2, hash G3, sync hash-update G5). See
   [cascade-modeling.md](reference/cascade-modeling.md) for the methodology.

## Retry on empty/truncated agent output (implemented)

Headless agent invocations occasionally return empty or minimal output even
when the prompt and CLI flags are correct. `headless_agent()` retries up to
`HEADLESS_EMPTY_OUTPUT_RETRIES` times (default 2, configurable via env var)
with `HEADLESS_EMPTY_OUTPUT_RETRY_DELAY_S` between attempts (default 2s).
Each attempt is logged as a warning so intermittent failures are visible.

## Retry on timeout (implemented)

When an agent hits its timeout limit, `headless_agent()` retries up to
`HEADLESS_TIMEOUT_RETRIES` times (default 1, configurable via env var)
before giving up. Each timeout attempt persists any partial stdout/stderr
to `Agents/logs/timeout-debug/` for post-mortem analysis by self-heal.
The first timeout is logged as a warning; subsequent timeouts are errors.

## Session transcript capture (implemented)

The JSONL logs capture the final agent output, token usage, and
pre-/post-processor results. Full session transcripts (tool calls, agent
reasoning, intermediate steps) are enabled by default. See
[session-transcript-capture.md](reference/session-transcript-capture.md)
for the full research analysis.

**To disable** for tasks that are stable and producing noisy transcripts,
add `transcript: false` to the task frontmatter:

```yaml
---
agent: agency copilot
transcript: false
schedule: "0 * * * *"
---
```

**Per agent type:**

| Agent | Mechanism | Transcript location |
|-------|-----------|-------------------|
| `copilot` | `--share <path>` flag | `Agents/logs/<name>-transcript.md` |
| `agency copilot` | `--share <path>` flag | `Agents/logs/<name>-transcript.md` |
| `claude` | Not yet available | Planned: `--output-format stream-json` |
| `agency claude` | Not yet available | Planned: `--output-format stream-json` |

**Behavior:**
- Enabled by default (`transcript: true`). Set `transcript: false` to disable
  for stable tasks that produce noisy transcripts
- The `--share` flag is version-checked via `agent_supported_flags()`
- Transcripts overwrite on each run (one file per task, not timestamped)
- Transcript path is recorded in the JSONL log entry (`transcript_path` field)
- Transcript path is referenced in self-heal GitHub issues when available
- Typical transcript size: 10-50 KB per run

**Retention policy:** One transcript per task (overwritten each run). If
timestamped retention is needed later, the transcript path can be made
configurable.

## Structured error categories (implemented)

JSONL error log entries now include an `error_category` field for automated
triage by `self-heal-log-alerts.py`. Categories:

| Category | When |
|----------|------|
| `timeout` | Agent timed out (`subprocess.TimeoutExpired`) |
| `cli_crash` | Agent exited with non-zero status or command not found |
| `output_parse_error` | JSON extraction from agent output failed |
| `pre_processor_crash` | Pre-processor exited with non-zero status |
| `handler_crash` | Handler (post-processor) exited with non-zero status |
| `agent_error` | Generic agent error (catch-all in `run.py`) |

The `error_category` field is optional -- only present on error entries. It
enables self-healing agents to route fixes by category without parsing
free-text error messages.

## Structured traceback extraction (implemented)

Error log entries that capture stderr now include a `traceback` field
(via `_extract_traceback()` in headless.py) containing the last Python
traceback from stderr, or `null` if none is found. This makes
tracebacks queryable in DuckDB without parsing the full `message` field.
`qlog.py --errors` automatically displays a "Tracebacks" section after
the main table, showing the last 20 lines of each traceback.

## stderr logging (updated)

stderr is now logged up to `MAX_LOG_FIELD_LENGTH` (20,000 chars) in all
`log_event()` calls. Previously truncated to 200 chars in most places,
which made tracebacks and diagnostic output invisible.

## Copilot output filtering (updated)

`filtered_copilot_output()` now keeps up to `COPILOT_OUTPUT_MAX_LINES`
(100) non-noise lines, up from the previous hard-coded limit of 20.
This reduces data loss for non-JSON tasks while still filtering
copilot UI noise (progress indicators, box drawing, status lines).

## Timeout

Agent CLIs (especially `agency copilot` via `script -qc`) can hang indefinitely
-- waiting for auth, MCP proxy startup, or simply never exiting after producing
output. `headless_agent()` wraps every invocation with a Python subprocess
timeout (default 120s, override via `HEADLESS_TIMEOUT`). Individual tasks can
set a custom timeout in their frontmatter with `timeout: <seconds>`.

## Agency `npx` MCP proxy requires `--package` and `--transport stdio`

Agency has a built-in `npx` MCP type that wraps npx-based MCP servers in a
proxy. Two critical details:

1. **`--package` is required.** The format is `npx --package <pkg>`, not
   `npx <pkg>`. Without `--package`, agency exits with status 2:
   `invalid arguments for --mcp 'npx'`.

2. **Defaults to HTTP transport.** The proxy appends `--transport http` to
   subprocess args. Stdio-only MCP servers (like `@softeria/ms-365-mcp-server`)
   don't understand this flag and fail with `MCP proxy for 'npx' did not output
   a valid port`. Fix: add `--transport stdio` before `--`.

`resolve_mcp()` in `headless.py` builds the correct format for each agent:

| Agent | `--mcp` flag format |
|-------|-------------------|
| `copilot`, `agency copilot` | `npx --package <pkg> --transport stdio -- <args>` |
| `claude`, `agency claude` | `npx --package <pkg> -- <args>` |

Copilot/agency copilot need `--transport stdio` to prevent the npx proxy
from defaulting to HTTP transport. Claude/agency claude run the `--mcp`
value as a literal shell command, so `--transport` would be passed to npx
itself (which doesn't support it) and cause a startup error.

Example resolved flag for `softeria-ms365`:
```
npx --package @softeria/ms-365-mcp-server --transport stdio -- --read-only
```

The MCP name in task frontmatter (`mcps: [softeria-ms365]`) is resolved against
`.mcp.json` servers. The `command`, `args`, and `env` fields are read
and transformed into the agent-specific `--mcp` flag. Environment variables
from `mcp.json` are injected into the agent's process env.

HTTP MCP entries in `.mcp.json` are resolved to Agency's remote-proxy
syntax instead of being passed through as bare workspace names. For example:

```
remote --url https://example.com/mcp --entra-client-id <oauthClientId>
```

This keeps triggered tasks aligned with the same authenticated remote MCP
configuration used by Agency for HTTP-based servers like WorkIQ-Mail.

## Agency copilot writes to stdout, not /dev/tty

Unlike bare `copilot` (which writes agent output to `/dev/tty`), `agency
copilot` writes to stdout. This means `agency copilot` does **not** need the
`script -qc` pseudo-tty wrapper -- standard `$(...)` capture works. In
`headless.py`, `agency copilot` has its own capture path that reads stdout
directly, then extracts JSON from either a markdown-fenced block
(`` ```json ... ``` ``) or bare JSON (`{...}`). The `run_copilot_with_pty()`
`script -qc` path is only used for bare `copilot`.

## Agency MCP proxy "failed to launch" warning is often benign

Agency tries to launch npx MCP proxies twice -- first with HTTP transport, then
falling back to stdio. The first attempt fails for stdio-only servers (like
`@softeria/ms-365-mcp-server`) with `Failed to launch proxy for MCP 'npx': MCP
proxy for 'npx' did not output a valid port`. The second attempt succeeds. The
error message is misleading -- the MCP is functional.

## MCP API rate limiting (429 errors)

Agents making parallel MCP calls to Microsoft Graph APIs get throttled (HTTP
429 `activityLimitReached`). Prompts using MS365 MCPs should instruct the
agent to query sequentially, not in parallel. The `todo-pull` prompt
includes this guidance explicitly.

## Copilot MCP arg quoting in `script -qc`

The `run_copilot_with_pty()` function builds a command string for `script -qc`.
MCP flag values containing spaces (e.g. `--mcp 'npx --package pkg -- args'`)
get word-split when interpolated into the string. Fix: use `printf %q` to
shell-escape each `--mcp` value before interpolation.

## Workspace MCP servers consume the entire context window

**Critical finding (April 2026).** The `copilot` and `agency copilot` CLIs
auto-load all MCP servers defined in `.mcp.json` on every session -- including
headless `-p` invocations. Each MCP server registers its full tool catalog
with the LLM. Large MCP servers (e.g. `@softeria/ms-365-mcp-server` with
174 tools for OneDrive, Calendar, Mail, Contacts, Planner, OneNote, Excel)
can consume **200K+ tokens** in tool definitions alone -- exceeding the 168K
context window and leaving zero room for conversation.

**Symptoms:**
- Agent crashes after 3-5 tool calls with `CAPIError: 400 messages.2.content.1:
  unexpected tool_use_id found in tool_result blocks`
- Session compaction fires immediately and on every turn
- `tool_definitions_tokens` in process logs shows 200K+
- The error appears to be a compaction bug but the root cause is context
  exhaustion from MCP tool definitions

**Root cause:** `--no-default-mcps` only disables Copilot's built-in defaults
(currently just `github-mcp-server`). It does **not** prevent workspace MCP
servers from `.mcp.json` from loading. There is no
`--no-workspace-mcps` flag.

**Fix in `headless.py`:** `build_agent_command()` reads `.mcp.json`,
enumerates all server names, and passes `--disable-mcp-server <name>` for
each server the task doesn't explicitly need (declared in `mcps:` frontmatter).
This is only applied for `copilot` and `agency copilot` agents (the flags are
copilot-specific and cause errors on the claude CLI).
This brought tool definition tokens from **211K -> 15K** (93% reduction) and
eliminated the compaction crashes.

```python
# workspace_mcp_server_names() reads .mcp.json server keys
wanted = set(config.mcps)
for server_name in workspace_mcp_server_names():
    if server_name not in wanted:
        command.extend(["--disable-mcp-server", server_name])
```

**Note:** This affects all `copilot` and `agency copilot` tasks, not just
headless runs. Interactive sessions in workspaces with large MCP configs
will also hit this limit eventually.

## WSL 9P bridge stability: cron tasks can crash the entire WSL instance

On WSL2, the Plan 9 (9P) filesystem bridge between Windows and Linux is
fragile under load. Triggered tasks can destabilize it and cause a
**crash-reboot loop** that takes down the entire WSL instance -- not just the
task. Three amplifiers were identified:

1. **`appendWindowsPath=true` (default).** Every command lookup probes 30+
   `/mnt/c/...` paths over 9P. Fix: set `appendWindowsPath=false` in
   `/etc/wsl.conf` `[interop]`. This hides Windows PATH from Linux but does
   **not** disable interop -- `.exe` files can still be called by full path.

2. **Windows git credential helper over 9P.** If `~/.gitconfig` has
   `credential.helper=/mnt/c/.../git-credential-manager.exe`, every `git push`
   invokes a Windows binary over the 9P bridge. Fix: use Linux-native
   `gh auth git-credential` instead.

3. **Duplicate MCP proxy processes via `--mcp` flags.** When a task's `mcps:`
   list includes servers already in the workspace `.mcp.json`, both the
   workspace autoload **and** the `--mcp` proxy flag spawn processes for the
   same server. The proxy processes invoke `msal.wsl.proxy.exe` (a Windows PE
   binary) for auth, hammering the 9P bridge. Fix: `resolve_task_config()`
   now skips `--mcp` flags for servers already in workspace config, letting
   the Copilot CLI's workspace autoload handle them.

**Symptoms of a 9P crash loop:**
- `dmesg` shows `Operation canceled @p9io.cpp:258 (AcceptAsync)` every ~37s
- Each p9io error triggers SIGTERM -> instance kill -> reboot
- Journal corruption on every boot (`corrupted or uncleanly shut down`)
- `workiq.exe` / `msal.wsl.proxy.exe` crashes with `STATUS_IN_PAGE_ERROR`
  (0xc0000006) because code pages can't be read over the broken 9P bridge

**Key `/etc/wsl.conf` settings for stability:**
```ini
[interop]
enabled=true
appendWindowsPath=false

[automount]
enabled=true
```

## `clean_path()` must resolve nvm in cron environments

Cron's minimal PATH doesn't include nvm directories. When the Copilot CLI
loads workspace MCP servers from `.mcp.json` and tries to spawn `npx`, it
fails with `spawn npx ENOENT`. `clean_path()` uses `shutil.which("node")`
to find the node bin directory, but this returns `None` under cron.

Fix: `clean_path()` now falls back to searching `~/.nvm/versions/node/*/bin/`
when `shutil.which("node")` fails, using the same glob pattern as
`build_stdio_params()` in `mcp_config.py`.

## Workspace MCP config: `.mcp.json` vs `.vscode/mcp.json`

Two MCP config files coexist in the repo:
- **`.vscode/mcp.json`** -- read by `headless.py` via `_load_mcp_servers()`
- **`.mcp.json`** -- auto-loaded by the Copilot CLI at runtime (created by
  Agency's vscode-mcp-migration)

`workspace_mcp_server_names()` (used for `--disable-mcp-server` flags) must
read **both** files to correctly identify which servers the Copilot CLI will
auto-load. Otherwise `--disable-mcp-server` targets the wrong set and
unwanted servers remain active.

## Operational reminders

- Each invocation is a **fresh session** -- no memory of previous runs.
- `--permission-mode plan` is enforced by the CLI, not the prompt.
- `--no-ask-user` is **not a valid Claude CLI flag** -- omit it. Copilot has it.
- Crontab persists across reboots. Watchers don't -- use `start` to restart.
- File watchers run in parallel -- rapid saves spawn multiple agents.
- All paths are Linux (Ubuntu on WSL), repo-relative where possible.

## Debounce with guaranteed flush (handler-level systemd timer pattern)

> **Not the same as Layer 2 above.** This section describes a pattern for
> _handlers_ that need their own debounce logic (e.g. buffering multiple
> operations before acting). The two-layer runtime debounce (Layer 1 + Layer 2)
> is separate and handles dispatch timing. This handler pattern is for
> tasks that receive dispatches and want to buffer work internally.

File-watcher handlers often need to buffer rapid edits and only act once the
user stops editing. The standard pattern:

1. On each file-change event, append operations to a `<task>.pending.json`
   buffer and reset `last_seen` to now.
2. On subsequent events, check if `last_seen` is older than `DEBOUNCE_SECONDS`.
   If so, flush the buffer and dispatch.
3. **Problem:** if no further file-change arrives, the buffer stalls forever.

**Solution:** schedule a transient systemd user timer as a guaranteed wake-up.
See `Taskflow/agents/handlers/taskflow-handler.py` for the reference
implementation (`schedule_debounce_timer`, `cancel_debounce_timer`,
`flush_all_pending`).

Key properties:
- **Sliding window:** each edit cancels the old timer, schedules a fresh one.
  Only one timer per task is outstanding at any time.
- **Deterministic unit names:** derived from the task filename so cancellation
  is reliable.
- **Guaranteed dispatch:** even if no further file events arrive, the timer
  fires and the handler re-runs in `--flush-pending` mode.
- **Prerequisites:** the watcher process must have `DBUS_SESSION_BUS_ADDRESS`
  and `XDG_RUNTIME_DIR` in its environment (inherited from the user session
  when started via `activate.py`). Systemd user services are a hard
  requirement -- there is no fallback path.
