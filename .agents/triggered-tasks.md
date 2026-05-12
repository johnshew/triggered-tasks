# Triggered Agents

Create, test, activate, and tear down scheduled (cron) and file-watch
triggered agents. Supports `claude`, `copilot`, `agency claude`,
`agency copilot`. Tasks can have multiple triggers (cron + watcher,
multiple watch paths) - each fires the pipeline independently.
Pre-handlers run before the agent for gating and enrichment.

Full instructions: `.claude/skills/triggered-task/SKILL.md`

## Layout

- `Agents/` - triggered agent definitions with frontmatter config
- `Agents/docs/` - design docs for multi-stage workflows
- `Agents/handlers/` - deterministic scripts run by triggered agents
- `Agents/logs/` - triggered agent execution logs
- `.claude/skills/triggered-task/docs/approach.md` - design notes

