# Release Process

> How to publish the triggered-task system as a standalone, shareable package.

---

## 1. Scope Definition

### Included in a release

| Area | Source path | Description |
|------|------------|-------------|
| Skill definition | `.claude/skills/triggered-task/` | `SKILL.md`, `docs/`, `scripts/` |
| Domain guide | `.agents/triggered-tasks.md` | Quick-reference for using triggered agents |
| Agent task prompts | `Agents/*.md` | Only tasks with `agent:` set to a real agent (not `none`). These demonstrate the full pipeline (pre-processor, agent, post-processor). |
| Referenced handlers | `Agents/handlers/` | Only handlers referenced by included task prompts (`pre-processor:` and `post-processor:` fields). |
| Infrastructure scripts | `Agents/scripts/` | Utility scripts |
| Example templates | *(generated)* | Synthetic examples for cron, watcher, handler-only, and agent tasks |
| Top-level config | `AGENTS.md`, `.editorconfig` | Repository-level guidance |

### Excluded from a release

| Area | Path | Reason |
|------|------|--------|
| Handler-only tasks | `Agents/*.md` with `agent: none` | No agent interaction to demonstrate; often contain personal data |
| Unreferenced handlers | `Agents/handlers/` | Only include handlers used by included task prompts |
| Runtime logs | `Agents/logs/` | User-specific execution data |
| Runtime data | `Agents/data/` | User-specific snapshots, indexes, tokens |
| Environment files | `.env`, `.env.*` | Secrets and credentials |
| Token caches | `~/.config/workiq-mail/`, `~/.config/ms365-mcp/` | User credentials |
| IDE state | `.vscode/`, `.obsidian/`, `.obsidian.mobile/` | User preferences |
| Git internals | `.git/` | Repository history |
| Personal notes | `Notes/`, `People/`, `Work/`, `Exercise/`, etc. | Private content |
| GitHub config | `.github/` | Repo-specific workflows and CI |

---

## 2. Release Target

The release repository has this layout:

```
triggered-task-release/
├── README.md                          # Getting started guide
├── AGENTS.md                          # Agent rules (sanitized)
├── .editorconfig
├── .claude/
│   └── skills/
│       └── triggered-task/
│           ├── SKILL.md
│           ├── docs/
│           │   ├── approach.md
│           │   ├── changelog.md
│           │   ├── commands.md
│           │   ├── diagnostics.md
│           │   ├── key-learnings.md
│           │   ├── release-process.md
│           │   └── reference/
│           │       ├── copilot-cli-headless-guide.md
│           │       ├── session-transcript-capture.md
│           │       └── wsl-runbook.md
│           └── scripts/
│               ├── activate.py
│               ├── assemble-release.sh
│               ├── headless.py
│               ├── logs.py
│               ├── pre-release-audit.py
│               ├── qlog.py
│               ├── run.py
│               ├── smoketest.py
│               ├── spawn.py
│               ├── status.py
│               ├── teardown.py
│               ├── test_headless.py
│               └── timeline.py
├── .agents/
│   └── triggered-tasks.md
├── Agents/
│   ├── docs/                          # Design docs (sanitized)
│   ├── handlers/                      # Example handlers
│   │   ├── write-files.sh
│   │   └── ...
│   ├── scripts/                       # Utility scripts
│   └── examples/                      # Template task prompts
│       ├── cron-example.md
│       ├── watcher-example.md
│       └── handler-only-example.md
└── Agents/logs/                       # Empty (gitkeep)
```

---

## 3. Release Steps

### 3.1 Pre-release audit

Run the pre-release audit script to scan for personal data and secrets:

```bash
uv run .claude/skills/triggered-task/scripts/pre-release-audit.py
```

The script checks for:
- Personal information (names, emails, hardcoded home paths)
- Secrets (API keys, tokens, `.env` references with values)
- Hardcoded local paths that break portability
- References to excluded runtime data

**Review required.** The script prints all findings. Some may be false
positives (e.g., `$HOME` in example commands). Fix real PII before
proceeding. If a finding is a known false positive, add a safe pattern
to `SAFE_PATTERNS` in the script.

### 3.2 Assemble release

```bash
bash .claude/skills/triggered-task/scripts/assemble-release.sh [output-dir]
```

The script:
1. Copies the skill definition, domain guide, and infrastructure scripts.
2. Scans `Agents/*.md` for tasks with `agent:` set to a real agent (not
   `none`). Only those prompts are included.
3. Extracts `pre-processor:` and `post-processor:` references from the
   included prompts and copies only those handler scripts.
4. Generates synthetic example templates (cron, watcher, handler-only,
   agent task).
5. **Prints the list of included task prompts and handlers for review.**
   Verify this list before proceeding -- it determines what personal
   content enters the release.

### 3.3 Manual review

1. **Browse the assembled output directory** (`/tmp/triggered-tasks`
   by default). The assemble script prints the full file tree and the list
   of included task prompts and handlers. Check for:
   - Personal data in task prompts (email addresses, names, user paths)
   - Handlers that reference personal infrastructure (MCP servers, APIs)
   - Any file that shouldn't be public
2. Verify the README is accurate for the release version.
3. Run the pre-release audit on the assembled output:
   ```bash
   cd /tmp/triggered-tasks
   uv run .claude/skills/triggered-task/scripts/pre-release-audit.py
   ```
4. Run the smoketest from the release directory:
   ```bash
   uv run .claude/skills/triggered-task/scripts/smoketest.py --agent claude
   ```
5. **Do not proceed to publish until you have reviewed the directory and
   are satisfied there is no personal content.**

### 3.4 Publish

Push the assembled directory to the release repository and tag:

```bash
cd /tmp/triggered-tasks
git init && git add .
git commit -m "Release vX.Y.Z"
git tag vX.Y.Z
git remote add origin <release-repo-url>
git push -u origin main --tags
```

---

## 4. Versioning

Use semantic versioning (`vMAJOR.MINOR.PATCH`):

| Change type | Version bump | Examples |
|-------------|-------------|----------|
| Breaking frontmatter schema changes | Major | New required fields, renamed fields |
| New features, new agent types | Minor | New commands, new handler dispatch |
| Bug fixes, doc updates | Patch | Typo fixes, edge case fixes |

Tag each release with `git tag vX.Y.Z` on the release commit.

---

## 5. Pre-release Checklist

- [ ] All unit tests pass (`python3 test_headless.py -v`)
- [ ] Pre-release audit script passes with no findings
- [ ] Documentation matches current behavior
- [ ] No personal data in any included file
- [ ] No secrets or credentials in any included file
- [ ] Example prompts demonstrate all task types (cron, watcher, multi, handler-only)
- [ ] README has accurate prerequisites and getting-started instructions
- [ ] Version tag follows semver convention

---

## 6. Design Docs — Release Inclusion

Not all design docs should ship. Include only those relevant to understanding
and operating the system:

| Doc | Include | Reason |
|-----|---------|--------|
| `approach.md` | Yes | Core architecture reference |
| `changelog.md` | Yes | Historical change log |
| `commands.md` | Yes | Command reference, CLI flags, log schema |
| `diagnostics.md` | Yes | Log inventory, diagnostic procedures, query recipes |
| `key-learnings.md` | Yes | Implementation gotchas and patterns |
| `release-process.md` | Yes | This document |
| `backlog.md` | No | Internal planning |
| `reference/copilot-cli-headless-guide.md` | Yes | Practical headless guide |
| `reference/session-transcript-capture.md` | Yes | Transcript capture techniques |
| `reference/wsl-runbook.md` | Yes | WSL operational runbook |
| `reference/agency cli.md` | Optional | External reference (may become stale) |
| `reference/cascade-modeling.md` | No | Methodology reference, not operational |
| `reference/session-transcript-capture.md` | No | Research (feature already implemented) |
