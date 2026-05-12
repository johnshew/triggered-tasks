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
