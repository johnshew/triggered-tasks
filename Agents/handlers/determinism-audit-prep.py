#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
"""
determinism-audit-prep.py — Pre-processor for determinism-audit.

Collects structured data that the agent would otherwise read from raw files:
  1. Task inventory — frontmatter from every Agents/*.md with agent != none
  2. Log statistics — per-task token usage, error rates, run counts from JSONL logs
  3. Prompt bodies — full prompt text for each agent task

Outputs JSON context so the agent receives pre-processed facts, reducing
prompt size by ~50-60%.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "Agents"
LOGS_DIR = AGENTS_DIR / "logs"
EXCLUDED_FILES = {"_index_.md"}
# Minimum number of completed runs (status ok or error) before recommending
# optimization for a task.  Tasks below this threshold are flagged as
# "insufficient data" rather than receiving a savings estimate.
MIN_RUNS_FOR_RECOMMENDATION = 3


def parse_frontmatter(path: Path) -> dict | None:
    """Extract YAML frontmatter from a markdown file."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return None
    end_index = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_index = i
            break
    if end_index is None:
        return None
    fm_text = "\n".join(lines[1:end_index])
    data = yaml.safe_load(fm_text)
    return data if isinstance(data, dict) else None


def extract_prompt_body(text: str) -> str:
    """Return everything after the YAML frontmatter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).strip()
    return text


def _parse_token_value(raw: str | None) -> float:
    """Convert token strings like '55.8k', '2.6M', '805' to float."""
    if not raw:
        return 0.0
    raw = raw.strip()
    m = re.match(r"^([\d.]+)\s*([kKmM]?)$", raw)
    if not m:
        return 0.0
    value = float(m.group(1))
    suffix = m.group(2).lower()
    if suffix == "k":
        value *= 1000
    elif suffix == "m":
        value *= 1_000_000
    return value


def read_log_entries(task_name: str) -> list[dict]:
    """Read JSONL log entries for a task."""
    log_path = LOGS_DIR / f"{task_name}.log"
    if not log_path.is_file():
        return []
    entries = []
    for line in log_path.read_text(encoding="utf-8").strip().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def compute_log_stats(entries: list[dict]) -> dict:
    """Compute statistics from log entries."""
    total_runs = 0
    successful_runs = 0
    error_runs = 0
    skipped_runs = 0
    total_tokens_in = 0.0
    total_tokens_out = 0.0
    total_premium_requests = 0.0
    total_cost_usd = 0.0
    durations: list[float] = []

    for entry in entries:
        phase = entry.get("phase")
        status = entry.get("status")

        if phase == "done":
            total_runs += 1
            if status == "ok":
                successful_runs += 1
            elif status == "skipped":
                skipped_runs += 1
            elif status == "error":
                error_runs += 1
            dur = entry.get("duration_s")
            if dur is not None:
                durations.append(float(dur))

        if phase == "agent" and status == "ok":
            total_tokens_in += _parse_token_value(entry.get("tokens_in"))
            total_tokens_out += _parse_token_value(entry.get("tokens_out"))
            pr = entry.get("premium_requests")
            if pr:
                try:
                    total_premium_requests += float(pr)
                except (ValueError, TypeError):
                    pass
            cost = entry.get("cost_usd")
            if cost:
                try:
                    total_cost_usd += float(cost)
                except (ValueError, TypeError):
                    pass

    stats: dict = {
        "totalRuns": total_runs,
        "successfulRuns": successful_runs,
        "errorRuns": error_runs,
        "skippedRuns": skipped_runs,
        "totalTokensIn": round(total_tokens_in),
        "totalTokensOut": round(total_tokens_out),
        "totalPremiumRequests": round(total_premium_requests),
    }
    if total_cost_usd > 0:
        stats["totalCostUsd"] = round(total_cost_usd, 4)
    if durations:
        stats["avgDurationS"] = round(sum(durations) / len(durations), 1)
    # Completed runs = successful + error (excludes skipped)
    completed_runs = successful_runs + error_runs
    stats["completedRuns"] = completed_runs
    stats["meetsMinRunThreshold"] = completed_runs >= MIN_RUNS_FOR_RECOMMENDATION
    # Per-run averages for model-downgrade analysis
    if completed_runs > 0 and total_premium_requests > 0:
        stats["avgPremiumPerRun"] = round(total_premium_requests / completed_runs, 2)
    return stats


def collect_task_inventory() -> list[dict]:
    """Enumerate agent tasks and collect frontmatter + log stats."""
    tasks = []
    for md_path in sorted(AGENTS_DIR.glob("*.md")):
        if md_path.name in EXCLUDED_FILES:
            continue
        fm = parse_frontmatter(md_path)
        if fm is None:
            continue
        agent = str(fm.get("agent", "agency copilot"))
        if agent == "none":
            continue

        task_name = md_path.stem
        prompt_body = extract_prompt_body(md_path.read_text(encoding="utf-8"))
        log_entries = read_log_entries(task_name)
        log_stats = compute_log_stats(log_entries)

        task_info: dict = {
            "name": task_name,
            "agent": agent,
            "mode": str(fm.get("mode", "plan")),
            "hasPreProcessor": bool(fm.get("pre-processor")),
            "preProcessor": fm.get("pre-processor"),
            "hasPostProcessor": bool(fm.get("post-processor")),
            "postProcessor": fm.get("post-processor"),
            "logStats": log_stats,
            "promptBody": prompt_body,
        }
        if fm.get("schedule"):
            task_info["schedule"] = str(fm["schedule"])
        if fm.get("watchPath"):
            task_info["watchPath"] = fm["watchPath"]
        if fm.get("mcps"):
            task_info["mcps"] = fm["mcps"]
        if fm.get("model"):
            task_info["model"] = str(fm["model"])

        tasks.append(task_info)
    return tasks


def main() -> int:
    tasks = collect_task_inventory()
    context = {
        "collectedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "minRunsForRecommendation": MIN_RUNS_FOR_RECOMMENDATION,
        "agentTasks": tasks,
        "taskCount": len(tasks),
    }
    print(json.dumps(context, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
