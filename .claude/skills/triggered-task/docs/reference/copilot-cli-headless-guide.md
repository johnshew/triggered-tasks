# Running GitHub Copilot CLI Headless: Practical Guide and Gotchas

> Status: **draft** — intended for public gist

A field guide to running `copilot` in headless mode (`-p`) for automated
tasks — cron jobs, file watchers, CI pipelines. Collected from building a
triggered-task system that runs agents on schedules.

## Basic Headless Invocation

```bash
copilot -p "summarize the README" --autopilot --no-ask-user
```

Key flags for unattended operation:

| Flag | Purpose |
|------|---------|
| `-p "prompt"` | Run headlessly with this prompt, exit when done |
| `--autopilot` | Don't ask for confirmation on tool use |
| `--no-ask-user` | Disable the `ask_user` tool (no interactive prompts) |
| `--no-custom-instructions` | Skip loading workspace instruction files |
| `--allow-all-tools` | Allow all tools (write mode) |
| `--deny-tool shell --deny-tool write` | Read-only mode (plan) |
| `--model claude-opus-4.6` | Override the default model |
| `--disable-builtin-mcps` | Disable Copilot's built-in MCP servers |
| `--disable-mcp-server <name>` | Disable a specific MCP server (repeatable) |

## Output Capture

The `copilot` CLI writes agent output to `/dev/tty`, not stdout, so
`$(copilot -p ...)` captures nothing useful. Wrap with `script` to capture
via a pseudo-tty:

```bash
script -qc 'copilot -p "your prompt" --autopilot --no-ask-user' /tmp/output.txt
output=$(cat /tmp/output.txt)
```

## Critical: Workspace MCP Servers Exhaust the Context Window

**This is the most important thing in this guide.**

The Copilot CLI auto-loads all MCP servers from `.mcp.json` on every
session — including headless `-p` invocations. Each MCP server registers its
full tool catalog, and those tool definitions count against your context
window.

MCP servers with large APIs (Microsoft Graph, databases, cloud providers)
can register hundreds of tools. In our case, workspace MCP servers contributed
**211K tokens** of tool definitions — more than the available context — before
the first message was even sent.

### Symptoms

- Agent crashes after 3–5 tool calls with a confusing error:
  ```
  CAPIError: 400 messages.2.content.1: unexpected `tool_use_id` found in
  `tool_result` blocks. Each `tool_result` block must have a corresponding
  `tool_use` block in the previous message.
  ```
- This *looks* like a session compaction bug, but the root cause is that tool
  definitions flood the context, forcing compaction on every turn. Compaction
  then corrupts the message history by removing `tool_use` blocks while
  keeping their `tool_result` responses.
- `--no-default-mcps` does **not** help. That flag only controls Copilot's
  built-in servers (currently just `github-mcp-server`), not workspace MCPs.

### How to Diagnose

Check `tool_definitions_tokens` in the session process log:

```bash
# Find the latest copilot session log directory
# Look in ~/.copilot/ or check the session log path printed at startup
grep 'tool_definitions_tokens' /path/to/session/process-*.log
```

If the number is above ~50K, workspace MCPs are eating your context.

### The Fix: `--disable-mcp-server`

Explicitly disable each workspace MCP server your task doesn't need:

```bash
copilot -p "your prompt" \
  --autopilot --no-ask-user \
  --disable-mcp-server my-large-mcp \
  --disable-mcp-server another-mcp
```

To automate this, read `.mcp.json` and disable everything not needed:

```python
import json, subprocess
from pathlib import Path

def get_workspace_mcp_names(repo_root: Path) -> list[str]:
    """Read MCP server names from .mcp.json."""
    mcp_config = repo_root / ".mcp.json"
    if not mcp_config.is_file():
        return []
    config = json.loads(mcp_config.read_text())
    return list((config.get("mcpServers") or config.get("servers") or {}).keys())

# Build headless command, disabling unwanted workspace MCPs
needed_mcps = set()  # MCPs this task actually needs; empty = none
cmd = ["copilot", "-p", prompt, "--autopilot", "--no-ask-user"]
for name in get_workspace_mcp_names(repo_root):
    if name not in needed_mcps:
        cmd.extend(["--disable-mcp-server", name])
```

### Results

| Metric | Before | After |
|--------|--------|-------|
| Tool definition tokens | 211K | 15K |
| Compaction crashes per run | Every run | None |

## Other Headless Gotchas

### VS Code IPC socket hijack

**Problem:** The Claude CLI auto-connects to VS Code via IPC sockets at
`/run/user/*/vscode-ipc-*.sock`. Neither `CLAUDE_CODE_AUTO_CONNECT_IDE=false`
nor clearing env vars prevents this.

**Solution:** `env -i` with minimal PATH. Implemented in `headless.py`:

```bash
env -i HOME="$HOME" PATH="$CLEAN_PATH" claude -p "..." --permission-mode plan
```

Only needed inside VS Code / Claude Code sessions. Cron invocations are unaffected.

### Reliable JSON output: checklist + magic value

Copilot inconsistently follows "output only JSON" instructions -- it may add
explanation, attempt shell commands, or stop after reading the file. The fix
is a structured checklist prompt with a verifiable magic value:

```markdown
## Steps

1. Read this prompt completely
2. Build the JSON object shown below
3. Keep the magic field exactly "xyzzy"
4. Keep the file entry exactly as shown
5. Output the JSON object only

## Required output

{"files":[...],"summary":"...","magic":"xyzzy"}
```

**Why this works:** The numbered steps force the agent to execute deliberately
instead of taking shortcuts. The magic value (`"magic":"xyzzy"`) provides a
machine-verifiable signal -- the validator checks `jq -e '.magic == "xyzzy"'`
to confirm the agent actually built the JSON rather than outputting garbage.
Keep the framing plain: describe it as a local smoketest and avoid language
about "pipeline" handling or dire warnings about non-JSON output. In April
2026 that wording started triggering false prompt-injection refusals from some
agents.

**Why plain "output only JSON" fails:** Without the checklist, copilot often
reads the prompt file, sees the JSON, and either (a) tries to execute it via
shell commands, (b) explains what it would do, or (c) outputs nothing after
the tool-use decoration.

Both smoketest prompts (`smoketest-cron` and `smoketest-watcher`) use this
pattern. See `smoketest.py` for the working implementation.

### Claude vs Copilot vs Agency Copilot

| | Claude | Copilot | Agency Copilot |
|--|--------|---------|----------------|
| Output goes to | stdout | /dev/tty | stdout |
| Capture method | `$(...)` | `script -qc` | `$(...)` |
| System prompt | `--append-system-prompt` | Not available | Not available |
| `--no-ask-user` | Not valid | Valid | Valid |
| JSON format | Raw | Raw | Markdown-fenced (`` ```json ``) |
| JSON reliability | High | Medium (needs checklist) | High |

### Empty output on first attempt

Headless invocations occasionally return empty or minimal output even with
correct flags. This is non-deterministic — retry usually works. Build
retry logic into your automation:

```python
for attempt in range(3):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.stdout.strip():
        break
    time.sleep(2)
```

### Timeouts

Agent CLIs can hang indefinitely waiting for auth, MCP proxy startup, or
simply never exiting. Always wrap with a timeout:

```bash
timeout 120 copilot -p "prompt" --autopilot --no-ask-user
```

### JSON output from agents is unreliable

Agent output includes tool-call progress lines, session chrome, and
sometimes markdown fencing. Don't rely on the output being clean JSON.
Either:
- Use a sentinel marker in your prompt (e.g. `RESULT:`) and scan for it
- Let the agent work naturally and extract JSON with a parser that
  tolerates surrounding noise

### MCP arg quoting with `script -qc`

When using `script -qc` to capture output, MCP flags with spaces get
word-split. Shell-escape them:

```bash
mcp_flag=$(printf '%q' "npx --package @my/mcp-server --transport stdio")
script -qc "copilot -p 'prompt' --mcp $mcp_flag --autopilot" /tmp/out.txt
```

## WSL Stability: Cron Tasks Can Crash the Entire Instance

On WSL2, headless cron tasks can destabilize the Plan 9 (9P) filesystem
bridge between Windows and Linux, causing a crash-reboot loop that takes
down the entire WSL instance.

### What happens

1. Task spawns MCP proxy or invokes Windows `.exe` over 9P
2. 9P bridge gets `AcceptAsync` cancellation (`p9io.cpp:258`)
3. WSL init sends SIGTERM, kills the instance
4. Instance reboots, cron fires again, repeat every ~37 seconds

### Prevention

1. **Set `appendWindowsPath=false`** in `/etc/wsl.conf` `[interop]` — stops
   30+ `/mnt/c/...` path probes on every command
2. **Use Linux-native git credentials** (`gh auth git-credential`) — never
   `credential.helper=/mnt/c/.../git-credential-manager.exe`
3. **Don't duplicate MCP servers** — if a server is in workspace `.mcp.json`,
   don't also pass it via `--mcp` flag (spawns duplicate proxy processes)
4. **Ensure `npx` is in PATH** — cron doesn't source nvm; resolve node
   binaries from `~/.nvm/versions/node/*/bin/` as a fallback

### Diagnosis

```bash
# Quick check from PowerShell
wsl -d Ubuntu -e bash -lc 'dmesg 2>/dev/null | grep -c p9io && dmesg 2>/dev/null | grep -c SIGTERM'
# Both should be 0
```

### Recovery

```powershell
wsl --shutdown          # Full VM restart
Start-Sleep 5
wsl -d Ubuntu -e bash -lc 'uptime; dmesg 2>/dev/null | grep -c p9io'
```

## What Would Help (Feature Requests)

1. **`--no-workspace-mcps`** — Disable all `.mcp.json` servers in one
   flag, analogous to `--disable-builtin-mcps` for built-in servers.
2. **Don't auto-load workspace MCPs in headless mode** — `-p` invocations
   should only load explicitly requested `--mcp` servers.
3. **Lazy tool loading** — Register tool definitions on first use rather than
   at session start (as suggested in
   [copilot-cli#2627](https://github.com/github/copilot-cli/issues/2627)).

## Environment

- Copilot CLI v1.0.22–1.0.24
- Ubuntu on WSL2
