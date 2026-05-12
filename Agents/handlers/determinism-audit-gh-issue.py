#!/usr/bin/env python3
"""Create a GitHub issue from a determinism audit report."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


class GitHubCliError(RuntimeError):
    """Raised when the GitHub CLI cannot complete a required operation."""


def _read_input() -> dict[str, object]:
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("no audit report received on stdin")
    # Try direct JSON parse first
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Fallback: extract JSON from markdown fences, heredocs, or shell wrappers
    # Try ```json ... ``` blocks
    fence_match = re.search(r'```(?:json)?\s*\n(.*?)\n```', raw, re.DOTALL)
    if fence_match:
        try:
            data = json.loads(fence_match.group(1).strip())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    # Try heredoc: cat << 'EOF' ... EOF  or  cat << 'AUDIT_EOF' ... AUDIT_EOF
    heredoc_match = re.search(r"<<\s*'?\w+'?\s*\n(.*?)(?:\n\w+\s*$|\Z)", raw,
                              re.DOTALL | re.MULTILINE)
    if heredoc_match:
        try:
            data = json.loads(heredoc_match.group(1).strip())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    # Try finding the first { ... } block
    brace_start = raw.find('{')
    if brace_start >= 0:
        # Find matching closing brace
        depth = 0
        for i in range(brace_start, len(raw)):
            if raw[i] == '{':
                depth += 1
            elif raw[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(raw[brace_start:i + 1])
                        if isinstance(data, dict):
                            return data
                    except json.JSONDecodeError:
                        break
    raise ValueError("could not extract JSON from agent output")


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def gh_output(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["gh", *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise GitHubCliError("gh CLI not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = _text(exc.stderr) or _text(exc.stdout)
        detail = " ".join(detail.split())
        command = " ".join(args[:2]) if len(args) >= 2 else " ".join(args)
        if detail:
            raise GitHubCliError(f"gh {command} failed: {detail}") from exc
        raise GitHubCliError(f"gh {command} failed with status {exc.returncode}") from exc
    return completed.stdout


def gh_json(*args: str) -> list[dict[str, object]]:
    stdout = gh_output(*args)
    data = json.loads(stdout or "[]")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def find_existing_issue(audit_date: str) -> dict[str, object] | None:
    marker = f"determinism-audit:{audit_date}"
    issues = gh_json(
        "issue",
        "list",
        "--state",
        "all",
        "--limit",
        "100",
        "--search",
        f"\"{audit_date}\"",
        "--json",
        "number,title,body,url,state",
    )
    for issue in issues:
        title = _text(issue.get("title"))
        body = _text(issue.get("body"))
        if marker in body or title == issue_title(audit_date):
            return issue
    return None


def issue_title(audit_date: str) -> str:
    return f"Triggered-task determinism audit opportunities ({audit_date})"


def _task_opportunities(data: dict[str, object]) -> list[dict[str, object]]:
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return []
    opportunities: list[dict[str, object]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if _text(task.get("recommendation")):
            opportunities.append(task)
    return opportunities


def render_issue_body(data: dict[str, object]) -> str:
    audit_date = _text(data.get("auditDate"), "unknown-date")
    marker = f"<!-- determinism-audit:{audit_date} -->"
    summary = _text(data.get("summary"), "No summary provided.")
    prioritized_actions = data.get("prioritizedActions")
    tasks = _task_opportunities(data)

    lines = [
        marker,
        "# Determinism audit opportunities",
        "",
        summary,
        "",
        "## Prioritized actions",
    ]

    if isinstance(prioritized_actions, list) and prioritized_actions:
        for action in prioritized_actions:
            if not isinstance(action, dict):
                continue
            priority = _text(action.get("priority"), "?")
            task = _text(action.get("task"), "unknown-task")
            text = _text(action.get("action"), "(no action provided)")
            lines.append(f"- **P{priority} — `{task}`**: {text}")
    else:
        lines.append("- No prioritized actions were provided.")

    lines.extend(["", "## Task-by-task recommendations"])
    for task in tasks:
        name = _text(task.get("name"), "unknown-task")
        agent = _text(task.get("agent"), "unknown-agent")
        savings = _text(task.get("estimatedSavings"), "not estimated")
        recommendation = _text(task.get("recommendation"), "(no recommendation provided)")
        deterministic_steps = task.get("deterministicSteps")

        lines.extend(
            [
                "",
                f"### `{name}`",
                f"- Agent: `{agent}`",
            ]
        )
        model = _text(task.get("model"))
        if model:
            lines.append(f"- Model: `{model}`")
        lines.extend(
            [
                f"- Estimated savings: {savings}",
                f"- Recommendation: {recommendation}",
            ]
        )

        if isinstance(deterministic_steps, list) and deterministic_steps:
            lines.append("- Deterministic steps worth moving:")
            for step in deterministic_steps:
                lines.append(f"  - {_text(step)}")

    return "\n".join(lines) + "\n"


def create_issue(title: str, body: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_path = Path(handle.name)
    try:
        return gh_output(
            "issue",
            "create",
            "--title",
            title,
            "--body-file",
            str(body_path),
        ).strip()
    finally:
        body_path.unlink(missing_ok=True)


def main() -> int:
    data = _read_input()
    audit_date = _text(data.get("auditDate"))
    if not audit_date:
        raise ValueError("auditDate is required")

    tasks = _task_opportunities(data)
    if not tasks:
        print("No determinism opportunities found; no issue created.")
        return 0

    existing = find_existing_issue(audit_date)
    if existing:
        print(f"Existing determinism audit issue already open: {_text(existing.get('url'))}")
        return 0

    title = issue_title(audit_date)
    body = render_issue_body(data)
    url = create_issue(title, body)
    print(f"Created determinism audit issue: {url}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GitHubCliError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
