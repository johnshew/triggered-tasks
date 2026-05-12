---
agent: agency copilot
mode: write
pre-processor: determinism-audit-prep.py
post-processor: determinism-audit-gh-issue.py
schedule: "0 9 * * 1"
timeout: 300
---

# Determinism Audit — Agent Task Review

Weekly audit of all triggered tasks that use CLI agents (`agent != none`).
Identifies opportunities to move deterministic work out of the agent and into
pre-processors or post-processors.

## Goal

Continuously improve the reliability and cost-efficiency of agent-driven tasks
by finding deterministic patterns that don't require LLM judgment.

## Pre-processor context

The pre-processor has already collected all structured data as JSON in the
`pre-processor="..."` field:

- **`agentTasks`** — array of every task with `agent != none`, including:
  - `name`, `agent`, `mode`, `schedule`, `watchPath`, `mcps`
  - `hasPreProcessor` / `hasPostProcessor` — whether processors are configured
  - `logStats` — token usage totals, run counts, error rates, skipped counts
  - `logStats.meetsMinRunThreshold` — whether the task has enough completed
    runs (≥ `minRunsForRecommendation`) to justify an optimization recommendation
  - `promptBody` — the full prompt text (everything after frontmatter)
- **`minRunsForRecommendation`** — minimum completed runs required before
  recommending optimization (currently 3)
- **`taskCount`** — total number of agent tasks found

You do NOT need to read any files from disk. All data is in the pre-processor
output.

## Steps

### 1. Review pre-processor data

Read the `pre-processor="..."` field. Parse the JSON. For each task in
`agentTasks`, note:
- Agent type and configuration
- Whether pre/post-processors already exist
- Log statistics: total runs, errors, skipped runs, token usage

If you need to see what an agent actually does during a run, check:
- `Agents/logs/runs/<task>-<timestamp>.stdout.txt` - full raw output from each run
- `Agents/logs/runs/<task>-<timestamp>.transcript.md` - archived session transcript (copilot/agency copilot agents)
- `Agents/logs/<task>-transcript.md` - live transcript (overwritten each run; use runs/ copy for history)

These are available on disk if you need to verify whether a step is
deterministic in practice.

### 2. Classify prompt steps

For each task, read its `promptBody`. Classify each step as:

**Deterministic** (could be a pre/post-processor script):
- Date/string parsing
- File reads/writes with predictable formats
- API calls that return structured data
- MCP use
- Conditional logic (if stale, if pending, if VIP)
- Shell commands
- Output formatting (JSON, frontmatter updates, markdown tables)

**Judgment** (should stay in the agent):
- Interpreting ambiguous content
- Drafting natural language
- Classifying intent when rules alone aren't sufficient
- Making context-dependent decisions
- Safety reviews requiring holistic understanding

### 3. Produce audit report

For each task where `logStats.meetsMinRunThreshold` is `false`, note the task
in the report but mark `estimatedSavings` as `"insufficient data — fewer than
N completed runs"` and omit specific optimization recommendations. The task
still appears in the audit for awareness, but no action is recommended until
enough runs accumulate.

Output a JSON summary:

```json
{
  "auditDate": "2026-04-22",
  "tasks": [
    {
      "name": "exercise-state-update",
      "agent": "agency copilot",
      "model": "claude-haiku-4.5",
      "has-pre-processor": true,
      "has-post-processor": true,
      "deterministicSteps": ["staleness check", "pipeline execution", "file writes"],
      "judgmentSteps": ["safety review", "agent commentary"],
      "recommendation": "Fully optimized: pre-processor gates 82% of triggers, haiku-4.5 reduced cost 4.5× (3.0→0.66 premium/run) and duration 3.2× (129s→41s). No further action.",
      "estimatedSavings": "none — already optimized"
    }
  ],
  "prioritizedActions": [
    {
      "priority": 1,
      "task": "flagged-email-monitor",
      "action": "Add pre-processor for context gathering; switch to haiku-4.5"
    }
  ],
  "summary": "N tasks audited. M have determinism opportunities."
}
```

### 4. Identify new opportunities

For any task that meets the minimum-run threshold and where deterministic steps
are currently done by the agent and no pre/post-processor exists:
- Describe what could move to a processor
- Estimate the token/cost savings
- Note the recommendation in the audit report

Also consider **model downgrades**. Tasks that perform structured extraction
or template filling (not creative writing) often work fine with a smaller model
like `claude-haiku-4.5`. The exercise-state-update switch from the default
model to haiku-4.5 cut cost 4.5× and duration 3.2× with no quality loss.
Check `logStats.avgPremiumPerRun` to identify high-cost tasks where a model
downgrade could help.

Populate the `prioritizedActions` array, ranking by estimated savings (highest
first). Each entry has `priority` (1 = highest), `task`, and `action`.

Do NOT create GitHub issues or modify any files directly. The post-processor
will create the tracking issue from your JSON output.

## Constraints

- Do NOT modify any task files, prompts, or processors
- Do NOT create files or issues directly
- Do NOT read files from disk — all data is in the pre-processor JSON
- Output only the JSON audit report
- Focus on actionable, specific recommendations
- Only recommend optimization for tasks with ≥ 3 completed runs
