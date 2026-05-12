#!/bin/bash
# assemble-release.sh — Copy release-included files into a clean directory.
#
# Usage:
#   bash Agents/scripts/assemble-release.sh [output-dir]
#
# Default output: /tmp/triggered-task-release

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
OUTPUT_DIR="${1:-/tmp/triggered-tasks}"

echo "Assembling release from: $REPO_ROOT"
echo "Output directory: $OUTPUT_DIR"

# Clean output
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

# --- Copy included directories ---

# Skill definition
mkdir -p "$OUTPUT_DIR/.claude/skills/triggered-task"
cp -r "$REPO_ROOT/.claude/skills/triggered-task/SKILL.md" "$OUTPUT_DIR/.claude/skills/triggered-task/"

# Copy scripts, excluding __pycache__
mkdir -p "$OUTPUT_DIR/.claude/skills/triggered-task/scripts"
for f in "$REPO_ROOT/.claude/skills/triggered-task/scripts/"*; do
  name="$(basename "$f")"
  if [ "$name" = "__pycache__" ] || [ -d "$f" ]; then
    continue
  fi
  cp "$f" "$OUTPUT_DIR/.claude/skills/triggered-task/scripts/"
done

# Skill docs (selective — exclude personal/unimplemented docs)
mkdir -p "$OUTPUT_DIR/.claude/skills/triggered-task/docs"
for doc in approach.md commands.md diagnostics.md key-learnings.md changelog.md release-process.md; do
  src="$REPO_ROOT/.claude/skills/triggered-task/docs/$doc"
  if [ -f "$src" ]; then
    cp "$src" "$OUTPUT_DIR/.claude/skills/triggered-task/docs/"
  fi
done
# Reference docs (selective)
mkdir -p "$OUTPUT_DIR/.claude/skills/triggered-task/docs/reference"
for doc in copilot-cli-headless-guide.md session-transcript-capture.md wsl-runbook.md; do
  src="$REPO_ROOT/.claude/skills/triggered-task/docs/reference/$doc"
  if [ -f "$src" ]; then
    cp "$src" "$OUTPUT_DIR/.claude/skills/triggered-task/docs/reference/"
  fi
done

# Domain guide
mkdir -p "$OUTPUT_DIR/.agents"
cp "$REPO_ROOT/.agents/triggered-tasks.md" "$OUTPUT_DIR/.agents/"

# Agents directory (agent task prompts + referenced handlers only)
mkdir -p "$OUTPUT_DIR/Agents/handlers"
mkdir -p "$OUTPUT_DIR/Agents/scripts"
mkdir -p "$OUTPUT_DIR/Agents/logs"

# Collect agent task prompts (agent: is set and not "none")
# and track which handlers they reference
INCLUDED_HANDLERS=()
INCLUDED_TASKS=()

for f in "$REPO_ROOT/Agents/"*.md; do
  name="$(basename "$f")"
  [ "$name" = "_index_.md" ] && continue

  # Extract agent field from YAML frontmatter
  agent=$(sed -n '/^---$/,/^---$/{ /^agent:/{ s/^agent: *//; s/ *$//; p; q; } }' "$f")
  [ -z "$agent" ] && continue
  [ "$agent" = "none" ] && continue

  cp "$f" "$OUTPUT_DIR/Agents/"
  INCLUDED_TASKS+=("$name (agent: $agent)")

  # Extract pre-processor and post-processor references
  for field in pre-processor post-processor; do
    handler=$(sed -n '/^---$/,/^---$/{ /^'"$field"':/{ s/^'"$field"': *//; s/ *$//; p; q; } }' "$f")
    [ -z "$handler" ] && continue
    INCLUDED_HANDLERS+=("$handler")
  done
done

# Copy only referenced handlers (deduplicated)
declare -A SEEN_HANDLERS
for h in "${INCLUDED_HANDLERS[@]}"; do
  [ -n "${SEEN_HANDLERS[$h]:-}" ] && continue
  SEEN_HANDLERS[$h]=1
  src="$REPO_ROOT/Agents/handlers/$h"
  if [ -f "$src" ]; then
    cp "$src" "$OUTPUT_DIR/Agents/handlers/"
  else
    echo "⚠️  Referenced handler not found: $h"
  fi
done

# Also copy write-files.sh (generic post-processor used by examples)
if [ -f "$REPO_ROOT/Agents/handlers/write-files.sh" ]; then
  cp "$REPO_ROOT/Agents/handlers/write-files.sh" "$OUTPUT_DIR/Agents/handlers/"
fi

# Copy top-level files
cp "$REPO_ROOT/AGENTS.md" "$OUTPUT_DIR/" 2>/dev/null || true
cp "$REPO_ROOT/.editorconfig" "$OUTPUT_DIR/" 2>/dev/null || true

# Keep logs dir with gitkeep
touch "$OUTPUT_DIR/Agents/logs/.gitkeep"

# --- Generate example task prompts ---

mkdir -p "$OUTPUT_DIR/Agents/examples"

cat > "$OUTPUT_DIR/Agents/examples/cron-example.md" << 'PROMPT'
---
agent: none
post-processor: write-files.sh
schedule: "0 9 * * *"
---

# Daily Report Generator

Collect data from the configured sources and produce a summary report.

## Output
Respond with ONLY a JSON object:
{
  "files": [
    { "path": "Reports/daily-report.md", "content": "# Daily Report\n\n..." }
  ],
  "summary": "Generated daily report"
}
PROMPT

cat > "$OUTPUT_DIR/Agents/examples/watcher-example.md" << 'PROMPT'
---
agent: none
post-processor: note-index.py
watchPath: Notes/
---

# Note Index Updater

When files change in the Notes/ directory, regenerate the index file.
PROMPT

cat > "$OUTPUT_DIR/Agents/examples/handler-only-example.md" << 'PROMPT'
---
agent: none
post-processor: my-handler.py
schedule: "*/30 * * * *"
---

# Periodic Sync

Run the handler every 30 minutes to sync data.
PROMPT

cat > "$OUTPUT_DIR/Agents/examples/agent-task-example.md" << 'PROMPT'
---
agent: agency copilot
mode: plan
pre-processor: my-prep.py
post-processor: write-files.sh
schedule: "0 */4 * * *"
timeout: 300
---

# Agent-Powered Analysis

Analyze the data prepared by the pre-processor and produce a report.

## Instructions

1. Read the pre-processor output for current data
2. Analyze trends and anomalies
3. Produce a summary report

## Output
Respond with ONLY a JSON object:
{
  "files": [
    { "path": "Reports/analysis.md", "content": "# Analysis\n\n..." }
  ],
  "summary": "Completed analysis"
}
PROMPT

# --- Generate README ---

cat > "$OUTPUT_DIR/README.md" << 'README'
# Triggered Task System

A framework for creating scheduled (cron) and file-watch triggered agent tasks.
Supports `claude`, `copilot`, `agency claude`, and `agency copilot` agents.

## Quick Start

### Prerequisites

- **Python 3.12+** and **[uv](https://docs.astral.sh/uv/)**
- **Node.js / npm** (for agent CLIs and MCP servers)
- **crontab** (for cron-scheduled tasks)
- **inotifywait** (for file-watcher tasks — `sudo apt install inotify-tools`)
- At least one agent CLI: `claude`, `copilot`, or `agency`

### Setup

1. Clone this repository
2. Check prerequisites:
   ```bash
   # Run the prereqs check (manual — see SKILL.md § prereqs)
   ```
3. Create your first task:
   ```bash
   # See Agents/examples/ for templates
   cp Agents/examples/cron-example.md Agents/my-task.md
   # Edit the prompt and frontmatter
   ```
4. Test it:
   ```bash
   uv run .claude/skills/triggered-task/scripts/run.py --name my-task
   ```
5. Activate it:
   ```bash
   uv run .claude/skills/triggered-task/scripts/activate.py --name my-task
   ```

### Commands

| Command | Script |
|---------|--------|
| Run once | `uv run .claude/skills/triggered-task/scripts/run.py --name <name>` |
| Activate | `uv run .claude/skills/triggered-task/scripts/activate.py --name <name>` |
| Status | `uv run .claude/skills/triggered-task/scripts/status.py` |
| Stop | `uv run .claude/skills/triggered-task/scripts/teardown.py --name <name>` |
| Logs | `uv run .claude/skills/triggered-task/scripts/logs.py [name]` |
| Smoketest | `uv run .claude/skills/triggered-task/scripts/smoketest.py` |

### Task Types

- **Cron**: Runs on a schedule (e.g. every hour, daily)
- **Watcher**: Runs when files change in a watched directory
- **Multi**: Both cron and watcher triggers on the same task
- **Handler-only** (`agent: none`): No LLM — runs a deterministic script

### Documentation

- [SKILL.md](.claude/skills/triggered-task/SKILL.md) — Full skill reference
- [Approach](.claude/skills/triggered-task/docs/approach.md) — Architecture design
- [Examples](Agents/examples/) — Template task prompts

## License

See [LICENSE](LICENSE) if included.
README

echo ""
echo "✅ Release assembled at: $OUTPUT_DIR"
echo ""
echo "=== Included task prompts ==="
for t in "${INCLUDED_TASKS[@]}"; do
  echo "  $t"
done
echo ""
echo "=== Included handlers ==="
for h in "${!SEEN_HANDLERS[@]}"; do
  echo "  $h"
done
[ -f "$OUTPUT_DIR/Agents/handlers/write-files.sh" ] && echo "  write-files.sh (generic)"
echo ""
echo "=== Release file tree ==="
find "$OUTPUT_DIR" -type f | sed "s|$OUTPUT_DIR/||" | sort
echo ""
echo "📂 Please review the assembled release directory before publishing:"
echo ""
echo "   $OUTPUT_DIR"
echo ""
echo "Check for personal content (emails, names, hardcoded paths) in the"
echo "included task prompts and handlers listed above."
echo ""
echo "When satisfied, run the pre-release audit on the assembled output:"
echo "   cd $OUTPUT_DIR && uv run .claude/skills/triggered-task/scripts/pre-release-audit.py"
