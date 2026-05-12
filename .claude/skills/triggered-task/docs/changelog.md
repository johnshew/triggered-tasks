# Changelog

Reverse-chronological log of significant infrastructure changes.

## 2026-05-09

- **WSL 9P crash loop fix.** Cron-triggered tasks were crashing the entire
  WSL instance via 9P bridge overload. Three changes: (1) `clean_path()` now
  searches `~/.nvm/versions/node/*/bin/` when `shutil.which("node")` fails,
  fixing `spawn npx ENOENT` in cron environments. (2) `workspace_mcp_server_names()`
  now reads both `.vscode/mcp.json` and `.mcp.json`, fixing incorrect
  `--disable-mcp-server` targeting. (3) `resolve_task_config()` skips `--mcp`
  flags for servers already in workspace config, preventing duplicate MCP proxy
  processes that invoked `msal.wsl.proxy.exe` over the 9P bridge.
- **Root cause analysis.** WSL 2.7.3.0 has infrastructure-level p9io errors
  (`AcceptAsync` cancellations) that are normally benign but become fatal when
  amplified by heavy 9P workload. The amplifiers were: Windows PATH probing
  (30+ `/mnt/c/` lookups per command), git-credential-manager.exe over 9P,
  and duplicate MCP proxy processes spawning Windows binaries. Full timeline
  and technical details captured in session checkpoint.

## 2026-05-07

- **Watcher debounce env fix.** `_debounce_schedule` in activate.py now
  passes `env=` containing synthesized `DBUS_SESSION_BUS_ADDRESS=
  unix:path=/run/user/<uid>/bus` and `XDG_RUNTIME_DIR=/run/user/<uid>`
  to every `systemctl --user` / `systemd-run --user` subprocess. Without
  this, cron-spawned watchers (the hourly health-check restarts dead
  watchers, inheriting cron's stripped env) couldn't reach user systemd
  and silently dropped every file-change dispatch with
  `error_category=systemd_unavailable`. New `_user_systemd_env()` helper
  preserves real session values when present (uses `setdefault`).
  (`5c3d0b0`)
- **Handler existence check during activation.** activate.py now verifies
  that the handler file referenced in a task definition exists before
  activating. Prevents cryptic runtime errors from config typos. (`7200d91`)
- **activate.py --all flag.** Iterates all task definitions and activates
  each that has a schedule or watchPath. Extracted `activate_one()` helper
  to reduce duplication between --name and --all paths. (`05a2bba`)
- **triggerStates emitted for all task types.** Removed
  `if config.task_type == "multi":` guard in `task_details()`. All types
  now uniformly report their trigger state in --json output. (`05a2bba`)

## 2026-05-05

- **Unified run output persistence.** `_persist_run_output()` replaces
  `_persist_timeout_debug()` and inline empty-output-debug logic. Saves
  full stdout/stderr for every run to `Agents/logs/runs/`. No truncation.
  Labels distinguish context: empty, timeout, transcript-fallback.
  (`96087b5`)
- **Constants hardened.** All 5 env-var-based tuning constants
  (`HEADLESS_TIMEOUT`, `HEADLESS_EMPTY_OUTPUT_RETRIES`,
  `HEADLESS_EMPTY_OUTPUT_RETRY_DELAY_S`, `HEADLESS_TIMEOUT_RETRIES`,
  `AGENT_HELP_TIMEOUT`) are now plain code constants. (`96087b5`)
- **Timeout retry.** `HEADLESS_TIMEOUT_RETRIES = 1` - on first timeout,
  log warning and retry once. All three execution paths (subprocess,
  streaming, copilot/pty) covered. (`4280d6c`)
- **Debounce timer collision fix.** `systemctl stop` now targets both
  `.timer` and `.service` units before scheduling a new timer.
  (`702de77`)

## 2026-05-04

- **Empty-output retry diagnostics and transcript fallback.** On empty
  output: classifies cause, attempts `--share` transcript JSON recovery,
  persists raw output, emits structured log with token stats. (`49310be`)
- **ensure-doc-convergence: agent: none.** Converted to fully
  deterministic pre+post-processor pipeline. (`c3780f1`)

## 2026-05-03

- **Dashboard NoneType fix.** Guard against None in
  `_taskflow_recent_activity` message handling. (`0de5b1f`)
- **ensure-doc-convergence JSON contract.** Tighter output validation
  prevents output_parse_error. (`936b6e0`)
- **error_category misclassification fix.** `run.py` now correctly
  classifies handler vs pre-processor errors when both keywords appear
  in the error message. (`067f7ce`)

## 2026-05-02

- **Deleted `cleanup-dispatch.md`.** Handler dispatch design is fully
  implemented in `build_handler_command()` / `_find_uv()` / PEP 723
  blocks. The 574-line planning doc was stale. Key decisions preserved
  in approach.md section 1 (handler dispatch paragraph).

## 2026-05-01

- **Task state: ground-truth only.** Removed log-based `active (log)`
  fallback from `task_state()` and `_trigger_states()` in headless.py.
  State is now determined solely by crontab entry (cron) or inotifywait
  PID (watcher). Stale log files no longer cause newly created tasks to
  appear active. Deleted dead code: `_last_run_age_seconds()`,
  `_cron_max_age_seconds()`. (`ad290e4`)
- **Spawn module moved to skill.** Relocated `Agents/lib/spawn.py` to
  `.claude/skills/triggered-task/scripts/spawn.py` (portable with the
  skill). Updated taskflow-handler import path. Added smoketest step
  10/12 validating find_uv() + spawn_agent_task() + child completion.
  (`ba4bcfb`)
- **Smoketest debounce fix.** Result file moved outside watched directory
  (was self-triggering the watcher). Test now fires at T=0, T=0.5s, T=5s
  to exercise both debounce layers (1s internal batch + 5s systemd timer
  reschedule). Added pre-flight inotifywait probe for fast sandbox
  failure. Documented unsandboxed requirement in SKILL.md. (`c6020e0`)
- **headless.py fenced_matches fix.** Changed `if not fenced_match` to
  `if not fenced_matches` - loop variable was unbound when code fence
  list was empty. (`aeee6b5`)
- **Release command.** Added `release` to SKILL.md CLI table with 4-step
  workflow (audit, assemble, verify, publish). (`aeee6b5`)
- **headless.py: debounce field.** New `debounce` frontmatter key parsed
  into TaskConfig. Delays watcher dispatch via systemd timer. (`795e383`)
- **headless.py: JSON repair.** New `_repair_json_quotes()` handles LLM
  output with unescaped inner quotes and invalid escape sequences
  (`\>`, `\*`). Iteratively fixes up to 50 errors. (`795e383`)
- **headless.py: multi-fence JSON extraction.**
  `extract_first_json_value()` now tries all code fences and picks the
  largest valid one. Falls back to repair on the largest failed fence.
  (`795e383`)

## 2026-04-30

- **headless.py hardening.** Expanded ANSI regex (OSC/DEC/charset),
  largest-dict JSON extraction fallback, stderr diagnostics on
  code-fence extraction failure. (`3897bbb`)

## 2026-04-29

- **Debounce timer pattern.** Added `systemd-run --user` transient timer to
  guarantee debounce flush even when no further file-change events arrive.
  Documented as a reusable pattern in key-learnings.md.

## 2026-04-28

- **headless.py path fix.** `parents[3]` -> `parents[4]` for mcp_config
  import after lib/ move. (`1c6333c`)
- **`agency.toml` MCP migration.** Agency CLI now reads MCP server config
  from `agency.toml` instead of `.vscode/mcp.json`. The migration prompt
  was blocking headless `agency copilot` runs (49-minute hang on
  interactive "Save to agency.toml? (y/N)"). Resolved by piping "y" to
  a test run. All `agency copilot` tasks now start cleanly.
- **Unified agent pipeline.** Replaced `taskflow-reeval.md` +
  `taskflow-instruction.md` with single `taskflow-agent.md`. Handler
  collects non-deterministic operations into an array and dispatches via
  `spawn_agent()`. Pre-processor extracts sections; post-processor applies
  results. Deleted 3 files, net code reduction.
- **Debounce at agent dispatch level.** The debounce now wraps the entire
  agent dispatch (not just instruction buffering). Every file-change event
  resets the timer. 2-minute quiet window before agent fires.
- **Comprehensive pipeline logging.** All 4 pipeline components (prep,
  agent, apply, handler) now log every step to stderr with tagged prefixes
  for correlation in `triggered-tasks.log`.
- **Backlog: CLI runner health check.** Added to `.agents/triggered-tasks.md`.
  Daily smoketest of `agency copilot` to catch broken upgrades, expired auth,
  interactive prompts, and MCP failures.

## 2026-04-26

- **Transcript capture on by default.** `TaskConfig.transcript` default
  changed from `false` to `true`. Tasks that are stable/noisy can opt out
  with `transcript: false` in frontmatter.
- **Shared checkbox helpers.** Extracted duplicated checkbox key-extraction
  logic from `exercise-state-prep.py` and `exercise-state-write.py` into
  `Agents/handlers/checkbox_helpers.py` -- single source of truth for
  `CB_RE`, `checkbox_key()`, and `extract_checkboxes()`.
- **G6 unknown-key debug logging.** `_restore_checkboxes()` now logs
  items present in the agent output but missing from the pipeline cache.
- **Stale hash-cache pruning.** `_save_watch_hashes()` in `activate.py`
  now drops entries for files that no longer exist on disk.
- **`recs_behind` documented as defense-in-depth.** Comment updated to
  explain the check can't fire in normal operation (pipeline writes B+R
  simultaneously) but is kept for partial-write resilience.
- **Content-hash cascade guard in watcher dispatcher.** `activate.py`
  now SHA-256 hashes changed files after debounce and drops unchanged
  files from the dispatch batch. Per-file filtering (not all-or-nothing).
  120-second cascade window -- outside the window, `touch` forces a run.
  Cache in `Agents/data/<task>-watch-hashes.json`. Hash prefixes logged
  on both skip and dispatch events.
- **`log_event()` field validation.** String fields longer than
  `MAX_LOG_FIELD_LENGTH` are truncated at the source with a `_truncated`
  flag added to the entry. Caller-supplied `ts` is silently dropped
  (always generated). Non-serialisable values fall back to `default=str`;
  if that fails, a `phase: log_event` error entry with `repr()` of the
  payload is written instead -- never losing the event.
- **`triggered-tasks-health-check.py` converted to stdout/stderr pattern.**
  Removed direct `log_jsonl()` file writes. Handler now writes
  diagnostics to stderr (captured by run.py) and a structured JSON
  summary to stdout (becomes handler output + `structured_output`).
  Single code path through the JSONL pipeline. Removed
  `_is_resolved_watcher_restart()` from self-heal prep -- no longer
  needed since the handler's own restart logic is opaque to self-heal;
  self-heal only fires when the handler exits non-zero (failed restarts).
- **`task` field in per-task log entries.** `run.py` now includes
  `task=<name>` in every JSONL entry written to per-task logs (via
  `tlog`/`tlog_start`/`tlog_end` wrappers). All existing log entries
  (live and archived) were backpatched on 2026-04-26 to include `task`,
  so the field is now universally present.
- **`error_category` on stage-level errors.** Handler and pre-processor
  failure `log_event` calls in `headless.py` now include
  `error_category="handler_crash"` / `"pre_processor_crash"`. Previously
  the category only appeared in `run.py`'s top-level catch-all.
- **Self-heal uses `error_category` for grouping.** `group_findings()`
  in `self-heal-log-alerts-prep.py` now groups by `error_category` when
  present, falling back to `message_prefix`. This produces more stable
  groups (e.g., all `timeout` errors together regardless of message text).
- **Self-heal includes tracebacks in GH issues.** `Finding` dataclass
  now carries a `traceback` field. The GH issue handler renders a
  deduplicated "Tracebacks" section with Python-highlighted code blocks.
  Raw findings also show `error_category` labels.
- **Structured output in log entries.** When `extract_first_json_value()`
  succeeds, the parsed JSON is carried as `structured_output` on
  `AgentResult` and written to the JSONL log. Tasks that already output
  JSON get a queryable structured field automatically; free-form tasks
  are unaffected (`null`, omitted from log). No task definitions or
  prompts needed changes.
- **Dead log file cleanup.** Deleted 6 dead/corrupted log files
  (`flagged-email-promote.log`, `git-pull.log`, `ms-todo-sync.log`,
  `task-index.log`, `todo-change-monitor.log`, `todo-sync.log`) and 9
  empty archive subdirectories from retired/renamed tasks.
- **qlog.py documentation.** Rewrote section 4 Querying section with full
  `qlog.py` reference: all flags, SQL examples, output formats, quick
  alternatives.
- **Logging docs consolidated.** Added cross-references in
  `flagged-email-debugging.md`, `agent-health-dashboard.md`, and
  `session-transcript-capture.md` pointing to approach.md section 4 as the
  single authoritative source for JSONL schema and querying.
- **Log rotation: first successful run.** 258,907 rows archived into 18
  monthly Parquet files (556 KB total). Live JSONL reduced from 190 MB to
  21 MB. No data loss verified via DuckDB row counts and date-range checks.
- **Log rotation activated.** `log-rotate` task (`Agents/log-rotate.md`)
  running on cron `0 3 * * 0` (Sunday 3 AM UTC). Pre-processor:
  `Agents/handlers/log-rotate.py`.
- **qlog.py Parquet archive support.** `build_view()` now reads
  `Agents/logs/archive/<name>/*.parquet` alongside live JSONL.
  Transparent -- no `--all` flag needed.
- **Traceback extraction.** `_extract_traceback()` helper in headless.py
  extracts the last Python traceback from stderr into a dedicated
  `traceback` JSONL field on 4 error paths (agent subprocess, agent
  streaming, handler, pre-processor).
- **`qlog.py --errors` enhancements.** Auto-includes `error_category`
  column and prints a Tracebacks section (last 20 lines each) after the
  main table.
- **New error categories.** Added `pre_processor_crash` and
  `handler_crash` to `run.py` error classification (previously lumped
  into `agent_error`).
- **`MAX_LOG_FIELD_LENGTH` raised to 20,000.** Was effectively ~200 in
  many places, hiding tracebacks and stderr content.

## 2026-04-23

- **Multi-directory file watches.** A single task can watch multiple
  paths; each fires the pipeline independently. Runner reports phase
  timing. (`c9ef5af`)

## 2026-04-16

- **Session transcript capture.** Full agent session transcripts logged
  for post-mortem analysis. Structured error categories. Full stderr
  logging. (`7046f8c`)
- **Smoketest false-failure fix.** Watcher shutdown noise no longer trips
  smoketest assertions. (`a4545b5`)
