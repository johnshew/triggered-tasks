# WSL Operational Runbook

Debugging, restarting, and verifying the triggered-task infrastructure when
things go sideways. All commands run from the Windows side unless noted.

## Quick health check (30-second triage)

```bash
# From Windows PowerShell -- run all checks in one shot
wsl -d Ubuntu --cd ~ -e bash -lc '
echo "=== WSL uptime ===" && uptime
echo "=== p9io errors (should be 0) ===" && dmesg 2>/dev/null | grep -c "p9io"
echo "=== SIGTERMs (should be 0) ===" && dmesg 2>/dev/null | grep -c "SIGTERM"
echo "=== Cron ===" && systemctl is-active cron
echo "=== Last 3 task runs ===" && tail -3 ~/repos/life/Agents/logs/triggered-tasks.log
echo "=== Interop ===" && (cmd.exe /c "echo OK" 2>/dev/null || echo "BROKEN")
'
```

**What good looks like:** uptime > few minutes, 0 p9io errors, 0 SIGTERMs,
cron active, recent task runs with `status: ok`, interop says OK.

## Diagnosing a crash loop

If WSL keeps restarting (shells die within seconds of opening):

```bash
# 1. Check if WSL is in a crash loop (from PowerShell)
wsl -d Ubuntu --cd ~ -e bash -lc 'dmesg --time-format iso 2>/dev/null | grep -E "p9io|SIGTERM|corrupted" | tail -20'

# 2. Count crash-restart cycles
wsl -d Ubuntu --cd ~ -e bash -lc '
echo "p9io errors: $(dmesg 2>/dev/null | grep -c p9io)"
echo "SIGTERMs: $(dmesg 2>/dev/null | grep -c SIGTERM)"
echo "Unclean shutdowns: $(dmesg 2>/dev/null | grep -c corrupted)"
'

# 3. Check journal for previous boots (shows crash history)
wsl -d Ubuntu --cd ~ -e bash -lc 'journalctl --list-boots --no-pager 2>/dev/null | tail -10'
```

**Key pattern:** `Operation canceled @p9io.cpp:258 (AcceptAsync)` followed
by SIGTERM 3s later = 9P bridge failure. Multiple boots within minutes =
crash loop.

## Nuclear restart (when WSL is unstable)

```powershell
# 1. Full shutdown (kills the VM, not just the distro)
wsl --shutdown

# 2. Wait 5 seconds for clean shutdown
Start-Sleep 5

# 3. Restart and verify
wsl -d Ubuntu --cd ~ -e bash -lc 'echo "Boot OK"; uptime; dmesg 2>/dev/null | grep -c p9io'
```

## Restarting the triggered-task system after a crash

After WSL restarts, cron auto-starts (systemd handles it), but file watchers
do not survive reboots. To restore everything:

```bash
# 1. Verify cron is running
wsl -d Ubuntu --cd ~ -e bash -lc 'systemctl is-active cron && echo "cron OK" || sudo systemctl start cron'

# 2. Check task health (what the hourly health-check does)
wsl -d Ubuntu --cd ~/repos/life -e bash -lc '
  uv run --script .claude/skills/triggered-task/scripts/status.py --json 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
for t in data.get(\"tasks\", []):
    status = t.get(\"status\", \"unknown\")
    name = t.get(\"name\", \"?\")
    trigger = t.get(\"trigger\", \"?\")
    print(f\"  {status:8s} {trigger:8s} {name}\")
"
'

# 3. Reactivate all watchers and cron tasks
wsl -d Ubuntu --cd ~/repos/life -e bash -lc '
  uv run --script .claude/skills/triggered-task/scripts/activate.py --all 2>&1
'

# 4. Run a quick smoketest to verify MCP connectivity
wsl -d Ubuntu --cd ~/repos/life -e bash -lc '
  timeout 60 uv run --script .claude/skills/triggered-task/scripts/run.py --name smoketest-mcp-personal 2>&1
'
```

## Verifying MCP servers work

```bash
# Handler path (direct stdio MCP call, no agent)
wsl -d Ubuntu --cd ~/repos/life -e bash -lc '
  timeout 30 uv run --script Agents/handlers/smoketest-mcp-work-prep.py 2>&1
'
# Should output: {"skip": false, "handler_result": "PASS", ...}

# Full agent path (spawns copilot CLI with MCP)
wsl -d Ubuntu --cd ~/repos/life -e bash -lc '
  timeout 180 uv run --script .claude/skills/triggered-task/scripts/run.py --name smoketest-mcp-work 2>&1
'
# Should output: SMOKETEST_RESULT: PASS - both paths working
```

## Checking credentials

```bash
# GitHub CLI auth (used for git push in git-sync)
wsl -d Ubuntu --cd ~ -e bash -lc 'gh auth status 2>&1'

# Git credential helper (should be gh, NOT git-credential-manager.exe)
wsl -d Ubuntu --cd ~ -e bash -lc 'git config --global --get-regexp credential 2>&1'
# Good:  credential.https://github.com.helper=!/usr/bin/gh auth git-credential
# Bad:   credential.helper=/mnt/c/.../git-credential-manager.exe  (9P bridge killer)
```

## Checking WSL interop

```bash
# Verify binfmt handler is registered
wsl -d Ubuntu --cd ~ -e bash -lc 'cat /proc/sys/fs/binfmt_misc/WSLInterop 2>/dev/null || echo "MISSING"'

# Verify .exe execution works
wsl -d Ubuntu --cd ~ -e bash -lc 'cmd.exe /c "echo INTEROP_OK" 2>&1'
```

If interop is missing after a config change or WSL update:
1. Verify `/etc/wsl.conf` has `[interop] enabled=true`
2. `wsl --shutdown` from PowerShell, then reopen terminal
3. If still missing: `sudo sh -c 'echo :WSLInterop:M::MZ::/init:PF > /proc/sys/fs/binfmt_misc/register'`

## Reading task logs

```bash
# Recent runs for a specific task
wsl -d Ubuntu --cd ~ -e bash -lc 'grep "smoketest-mcp-work" ~/repos/life/Agents/logs/triggered-tasks.log | tail -5'

# Task-specific log
wsl -d Ubuntu --cd ~ -e bash -lc 'tail -20 ~/repos/life/Agents/logs/smoketest-mcp-work.log'

# Run artifacts (stdout, stderr, transcripts)
wsl -d Ubuntu --cd ~ -e bash -lc 'ls -lt ~/repos/life/Agents/logs/runs/smoketest-mcp-work-*.* | head -5'

# Errors across all tasks in last hour
wsl -d Ubuntu --cd ~ -e bash -lc '
  grep ""level":"error"\|"status":"error"\|"status":"timeout"" ~/repos/life/Agents/logs/triggered-tasks.log | tail -10
'
```

## Known failure modes and fixes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `spawn npx ENOENT` in agent logs | nvm not in cron PATH | Fixed in `clean_path()` -- searches `~/.nvm/` |
| `Exec format error` for `.exe` | WSL interop disabled | Check `wsl.conf [interop] enabled=true`, restart |
| `Authentication failed` from workiq | MSAL token expired | Run `workiq ask` interactively once to re-auth |
| p9io crash loop every ~37s | 9P bridge overload | Check for Windows PATH, credential helper, duplicate MCPs |
| Watchers dead after reboot | Watchers don't survive reboot | `activate.py --all` or wait for hourly health check |
| `tool_definitions_tokens > 50K` | Workspace MCPs flooding context | Verify `--disable-mcp-server` flags in task command |
| Empty agent output | Non-deterministic CLI behavior | Built-in retry (1 attempt); check `Agents/logs/runs/` |
| OOM kills in dmesg | Memory exhaustion | Check `.wslconfig` limits; look for runaway inotifywait |

## Configuration files reference

| File | Location | Purpose |
|------|----------|---------|
| `wsl.conf` | `/etc/wsl.conf` (Linux) | Per-distro: interop, automount, boot |
| `.wslconfig` | `C:\Users\<user>\.wslconfig` | VM-level: memory, swap, experimental |
| `.mcp.json` | Repo root (Linux) | Copilot CLI auto-loads these MCP servers |
| `.vscode/mcp.json` | Repo root (Linux) | headless.py reads MCP configs from here |
| `~/.gitconfig` | Linux home | Git credential helper config |
| `Agents/data/desired-tasks.json` | Repo (Linux) | Health check compares against this |
| `Agents/data/health.ok` | Repo (Linux) | Written by health check when all tasks healthy |
