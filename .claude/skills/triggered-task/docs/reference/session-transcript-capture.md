# Research: Full CLI Session Transcript for Triggered Tasks

> **Status**: Research complete, key recommendations implemented.
> `--share` transcript capture is live in `headless.py`.
> **Issue**: #101
> **Date**: April 2026 (updated 2026-05-12)

---

## Problem

Triggered tasks (headless runs via Agency + Copilot CLI) produce only a
summary-level log (see
[triggered-task approach.md § 4. Logging](../../.claude/skills/triggered-task/docs/approach.md#4-logging)
for JSONL schema and querying). The JSONL entries in `Agents/logs/<name>.log`
record `phase: agent` with the final output, token usage, and status — but
not the full conversation: tool calls, tool results, agent reasoning steps, or
intermediate outputs.

Agency's file sink (`client/agency/src/session_manager/sinks/file.rs`)
redacts `chat.json` and `events.jsonl` content when writing session logs to
`~/.agency/logs/session_*/`. The unredacted content only survives in the raw
`agency_copilot_*.log` file, embedded as JSON in TRACE-level `hook.start`
events.

---

## Deep Analysis: Current Capture Pipeline

### Architecture Overview

```
activate.py (cron/watcher lifecycle)
  └── run.py (single execution)
        ├── run_pre_processor()     → headless.py
        ├── headless_agent()        → headless.py
        │     ├── copilot: run_copilot_with_pty()   → script -qc + PTY
        │     ├── agency copilot: run_copilot_with_pty()
        │     ├── claude/agency claude (quiet): subprocess.run(capture_output)
        │     └── claude/agency claude (stream): _run_agent_streaming()
        └── run_post_processor()    → headless.py
```

### What Gets Captured Per Agent Type

#### `copilot` (bare) — PTY capture via `script -qc`

| Data | Captured | How | Limitation |
|------|----------|-----|------------|
| Agent output | ✓ | PTY transcript file (ANSI-stripped) | `filtered_copilot_output()` keeps only first 20 non-noise lines when no JSON found |
| stderr | ✓ | `completed.stderr` from `script` subprocess | Logged as `[:500]` only |
| Tool calls | ✗ | Not in stdout — only visible in session logs | Lost to redacted `~/.agency/logs/` |
| Agent reasoning | ✗ | Not in stdout — only visible in session logs | Same |
| Token usage | ✓ | Parsed from stderr via `parse_usage_stats()` regex | Fragile regex matching |

#### `agency copilot` — stdout capture (no PTY needed)

| Data | Captured | How | Limitation |
|------|----------|-----|------------|
| Agent output | ✓ | `subprocess.run(capture_output=True)` or `_run_agent_streaming()` | Same as copilot when JSON extraction fails: 20-line limit |
| stderr | ✓ | `completed.stderr` or `proc.stderr.read()` | Logged as `[:500]` only |
| Tool calls | ✗ | Not in stdout | Lost to redacted session logs |
| Agent reasoning | ✗ | Not in stdout | Same |
| Token usage | ✓ | Parsed from stderr via `parse_usage_stats()` regex | Fragile regex matching |

Unlike bare `copilot` (which writes to `/dev/tty` and needs PTY wrapping),
`agency copilot` writes to stdout. In `headless_agent()` (line 726), only
bare `copilot` takes the `run_copilot_with_pty()` path. Agency copilot
takes the normal `subprocess.run(capture_output=True)` path (non-streaming)
or `_run_agent_streaming()` (streaming).

#### `claude` / `agency claude` (non-streaming, `--quiet` mode)

| Data | Captured | How | Limitation |
|------|----------|-----|------------|
| Agent output | ✓ | `subprocess.run(capture_output=True)` stdout | `--output-format json` wraps output in JSON envelope |
| stderr | ✓ | `completed.stderr` | Logged as `[:500]` |
| JSON envelope | ✓ | `parse_claude_json_output()` extracts `result`, `usage`, `cost_usd`, `model` | JSON metadata discarded after extraction |
| Tool calls | ✗ | Not in JSON envelope | Lost — `--output-format json` only returns final result |
| Agent reasoning | ✗ | Not in JSON envelope | Same |
| Token usage | ✓ | Extracted from JSON `usage` field | High fidelity — structured data |

#### `claude` / `agency claude` (streaming, interactive mode)

| Data | Captured | How | Limitation |
|------|----------|-----|------------|
| Agent output | ✓ | `_run_agent_streaming()` tees stdout to terminal + collects lines | Full output captured |
| stderr | ✓ | Read after `proc.wait()` | Full stderr captured |
| Tool calls | ✗ | Not in stdout stream | Same limitation |
| Agent reasoning | ✗ | Not in stdout stream | Same limitation |

### Invocation Context Analysis

#### Cron invocations

```bash
# Generated cron line (from activate.py install_cron_task()):
0 * * * * cd /repo && uv run --script .../run.py --name my-task --quiet 2>&1
```

| Data | Fate |
|------|------|
| run.py stdout | `--quiet` suppresses most output; `2>&1` merges stderr |
| run.py stderr | Combined with stdout via `2>&1` |
| Combined output | Handled by cron daemon (MAILTO or discarded) |
| JSONL logs | ✓ Written directly by run.py, independent of stdout |

**Key insight:** The `--quiet` flag in cron lines means run.py suppresses
its own console output. All meaningful data reaches the JSONL log
via `log_event()` calls, which write directly to disk — unaffected by
stdout/stderr redirection.

#### Watcher invocations

```python
# activate.py watch_loop() spawns run.py:
subprocess.run(
    [sys.executable, str(RUN_ONCE_PATH), "--name", name,
     "--changed-file", chosen, "--quiet"],
    cwd=repo_root(),
    stdin=subprocess.DEVNULL,
    check=False,
)
```

| Data | Fate |
|------|------|
| run.py stdout | Inherited from watcher process — which has stdout/stderr redirected to `/dev/null` (via `activate_watcher()` devnull_fd, line 305-316) |
| run.py stderr | Same — goes to `/dev/null` |
| JSONL logs | ✓ Written directly by run.py — unaffected |
| return code | Ignored (`check=False`) — errors only captured via JSONL |

**Key insight:** Watcher-spawned runs discard all terminal output
(intentional — the watcher runs as a daemon). JSONL logging is the only
record. This means the JSONL log quality is critical for watcher tasks.

### What's Captured in JSONL Logs Today

A successful task run produces these JSONL entries:

```jsonl
{"ts":"...","phase":"start","trigger":"cron","agent":"agency copilot","mode":"plan",...}
{"ts":"...","phase":"pre-processor","status":"ok","output":"..."}
{"ts":"...","phase":"agent","status":"ok","output":"<final text>","model":"...","tokens_in":"...","tokens_out":"..."}
{"ts":"...","phase":"post-processor","status":"ok","message":"wrote 2 files"}
{"ts":"...","phase":"done","status":"ok","duration_s":15.0}
```

**What the JSONL captures well:**
- ✓ Final agent output text (up to 1 MB — `MAX_LOG_FIELD_LENGTH`)
- ✓ Token usage (model, tokens_in, tokens_out, tokens_cached)
- ✓ Cost (claude agents only, `cost_usd`)
- ✓ Premium request count (copilot agents, `premium_requests`)
- ✓ Pre-processor output (full, up to 1 MB)
- ✓ Post-processor status
- ✓ Error messages (stderr first 500 chars on failure)
- ✓ Duration
- ✓ Trigger context (cron/file-change/manual, changed_file)

**What the JSONL does NOT capture:**
- ✗ Tool calls the agent made (read_file, edit_file, bash, etc.)
- ✗ Tool results returned
- ✗ Agent reasoning/thinking steps
- ✗ Intermediate agent messages
- ✗ Full stderr on success (only first 500 chars logged)
- ✗ Session transcript / conversation history

### Recent Fixes That Improved Capture

| Commit | What it fixed |
|--------|---------------|
| `c86462e` — stderr diagnostics | stderr excerpts + log context included in error messages |
| `62aac55` — token usage logging | Parse token/model/cost from claude JSON + copilot stderr |
| `0f0567d` — watcher JSONL logging | Watcher errors go to JSONL, not `.out` files |
| `81bcca3` — SIGTERM handling | Clean watcher shutdown without spurious error logs |
| `88115e4` — `--no-custom-instructions` | Prevents Copilot from reloading repo instructions in headless mode |
| `3483f25` — CLI-version-aware flags | Detects `--no-default-mcps`/`--disable-mcp-server` support before using them |

**Assessment:** These fixes significantly improved the operational logging
quality (errors, usage, lifecycle). But they did not address the core gap:
**no tool-call or reasoning transcript is captured**.

---

## Weaknesses in the Current Approach

### 1. No conversation transcript

The fundamental gap. For any agent type, we capture the final output text
but not the intermediate steps. When debugging a task that produced wrong
output, there's no way to see what the agent read, what tools it called,
or how it reasoned.

### 2. Copilot output filtering is lossy

`filtered_copilot_output()` keeps only the first 20 non-noise lines and
applies a static list of noise prefixes (●, │, └, ✗, etc.). If the agent's
actual response is longer than 20 lines or starts with a filtered prefix,
data is silently lost. This filter is only applied when JSON extraction
fails — but for non-JSON tasks (analysis, reporting), it's the primary
extraction path.

### 3. stderr truncation

`log_event(config.task_log, phase="agent", level="info", message=stderr_text[:500])`
truncates stderr to 500 chars. For copilot/agency copilot, stderr contains
the usage summary (which is parsed separately) but may also contain
diagnostic info, MCP proxy logs, or auth messages that are lost.

### 4. PTY vs stdout confusion

The `headless_agent()` function has three distinct capture paths:
- `run_copilot_with_pty()` — for bare `copilot` only (line 726-727)
- `_run_agent_streaming()` — for all agents when `stream=True` (line 728-729)
- `subprocess.run(capture_output=True)` — for all agents when `stream=False` (line 730-739)

The bare `copilot` path is the only one that needs PTY wrapping (because
copilot writes to `/dev/tty`). Agency copilot writes to stdout directly.
This is correctly handled — agency copilot goes through the normal
subprocess paths.

### 5. Streaming mode discards structured output parsing

When `stream=True` (interactive/non-quiet mode), `_run_agent_streaming()`
tees stdout to the terminal and collects it. But the streaming path cannot
use `capture_output=True` — it reads stdout line-by-line. This means for
claude agents, the `--output-format json` envelope is still captured
(it's all on stdout), but the display shows raw JSON in the terminal
rather than pretty output.

### 6. No structured result extraction

The sentinel-output design (`TRIGGERED_TASK_RESULT:` marker) was proposed
but never implemented. Currently,
`normalize_agent_output()` uses heuristic JSON extraction with fragile
regex/prefix filtering. This works well enough for JSON-emitting tasks
but can't handle multi-format or non-JSON outputs cleanly.

---

## Research Findings (Original 5 Areas)

### 1. Agency Config — Redaction Bypass

**Finding: No user-facing flag to disable redaction.**

Agency's CLI exposes `--verbosity <LEVEL>` (log level) and `--log-dir <DIR>`
(log directory), but no `--unredacted-logs`, `--no-redaction`, or equivalent
flag. The `agency config list` / `agency.yaml` / `agency.toml` configuration
surface does not document a redaction toggle.

The redaction logic is in the Rust source (`sinks/file.rs`) and appears to be
a compile-time or feature-flag decision (`SESSION_STORE`,
`CLOUD_SESSION_STORE` features), not a runtime user option.

**Conclusion**: Not currently possible without Agency source changes.

### 2. Copilot CLI `--share` Flag

**Finding: The `--share` flag writes a full session transcript to a markdown
file. This is the most promising built-in approach.**

Both Copilot CLI and Agency Copilot support:

```bash
agency copilot -p "..." --share /path/to/transcript.md
copilot -p "..." --share /path/to/transcript.md
```

What gets exported:
- User prompts and agent responses
- Tool calls and file operations
- Model reasoning displayed during the session

The flag is already documented in the
[Agency CLI Reference](../../.claude/skills/triggered-task/docs/agency%20cli.md)
but is not currently used by the triggered-task runtime.

**Integration path**: Add `--share <path>` to `build_agent_command()` in
`headless.py`:

1. Add `--share` to the command for copilot / agency copilot agents
2. Write transcript to `Agents/logs/<name>-transcript.md` (overwrite each run)
3. Consider opt-in via frontmatter flag (e.g., `transcript: true`)

**Effort**: Small — ~10 lines in `headless.py` plus retention policy.

**Limitation**: Only covers copilot-based agents. Claude agents would need
a separate approach (Claude CLI does not have `--share`).

### 3. Hook Events in Raw Logs

**Finding: Full tool-call data exists in `~/.agency/logs/agency_copilot_*.log`
but requires parsing. More fragile than `--share`.**

The raw Agency log file contains TRACE-level entries with full tool
arguments and results. A post-processor could extract a transcript, but
the format is undocumented and may change between Agency versions.

**Not recommended** — `--share` is simpler and more reliable.

### 4. Sentinel-Based Structured Result (TRIGGERED_TASK_RESULT)

**Finding: Designed but not implemented. See
**See recommendations section below for current status.**

This addresses a different problem than transcript capture: returning
structured results from agent runs. The agent emits
`TRIGGERED_TASK_RESULT:` followed by JSON, and the runner extracts it.

Currently `normalize_agent_output()` uses heuristic JSON extraction
(regex, prefix filtering). The sentinel approach would be cleaner but
is orthogonal to transcript capture.

### 5. Session Store

**Finding: No user-accessible session store API.**

`SESSION_STORE` and `CLOUD_SESSION_STORE` are internal Agency features.
`--copilot-session-file` and `--copilot-log-name` control file names but
content is still redacted. Not a viable path.

---

## Recommendations

### 1. Integrate `--share` for Copilot/Agency Copilot (IMPLEMENTED)

The `--share` flag is now integrated in `headless.py`. When `transcript: true`
(the default) and the agent supports `--share`, the runner automatically
captures a full session transcript to `Agents/logs/<name>-transcript.md`.
Transcripts are archived per-run to `Agents/logs/runs/`.

To disable for a specific task, set `transcript: false` in frontmatter.

### 2. Log Full stderr (Not Just 500 Chars)

Currently stderr is truncated to 500 chars. Increase to match
`MAX_LOG_FIELD_LENGTH` (1 MB) for the `phase: agent` log entry.
Stderr often contains useful diagnostic data beyond the usage summary.

### 3. Consider Claude `--output-format stream-json` for Transcripts

Claude CLI supports `--output-format stream-json` which emits JSONL
events during the session. This could provide tool-call-level
granularity for claude/agency claude agents, analogous to `--share`
for copilot agents. Needs investigation — may conflict with the
current `--output-format json` used for result extraction.

### 4. Implement Sentinel Output (Separate Effort)

The `TRIGGERED_TASK_RESULT:` sentinel design is
sound. Implement it when structured data return becomes a priority.
Independent of transcript capture.

---

## Summary Table

| Approach | Feasibility | Effort | Fragility | Recommendation |
|----------|------------|--------|-----------|----------------|
| `--share` flag (copilot) | High | Small | Low (supported CLI flag) | **Do this first** |
| `--output-format stream-json` (claude) | Medium | Medium | Medium (format may evolve) | Investigate |
| Sentinel output extraction | High | Small-Med | Low | Separate effort |
| Increase stderr log limit | High | Trivial | None | **Do this** |
| Agency redaction config | None | N/A | N/A | Not available |
| Raw log parsing | Medium | Medium | High (undocumented format) | Skip |
| Session store API | None | N/A | N/A | Not available |

## Capture Pipeline Summary

| Data | copilot | agency copilot | claude | agency claude | Notes |
|------|---------|----------------|--------|---------------|-------|
| Final output | ✓ PTY | ✓ stdout | ✓ JSON | ✓ JSON | Up to 1 MB |
| Tool calls | ✗ | ✗ | ✗ | ✗ | Core gap — `--share` or `stream-json` would fix |
| Reasoning | ✗ | ✗ | ✗ | ✗ | Same |
| Tokens | ✓ stderr regex | ✓ stderr regex | ✓ JSON | ✓ JSON | Good coverage |
| Cost | ✗ | ✗ | ✓ JSON | ✓ JSON | Copilot uses premium requests |
| stderr | ✓ [:500] | ✓ [:500] | ✓ [:500] | ✓ [:500] | Should increase limit |
| Trigger context | ✓ | ✓ | ✓ | ✓ | cron/file-change/manual, changed_file |
| Pre-processor | ✓ | ✓ | ✓ | ✓ | Full output logged |
| Post-processor | ✓ | ✓ | ✓ | ✓ | Status + stderr logged |
