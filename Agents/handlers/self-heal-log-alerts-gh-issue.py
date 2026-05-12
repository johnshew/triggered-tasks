#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Post-processor for self-heal-log-alerts.

Reads the agent's structured JSON diagnosis from stdin and creates a
GitHub issue for each actionable finding group. Skips groups the agent
classified as noise. Uses pattern-based markers in the issue body so
that the pre-processor can match on future runs.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


_ENSURED_LABELS: set[str] = set()


class GitHubCliError(RuntimeError):
    """Raised when the GitHub CLI cannot complete a required operation."""


def _log(message: str) -> None:
    print(f"[self-heal-gh-issue] {message}", file=sys.stderr)


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _read_input() -> dict[str, object]:
    raw = sys.stdin.read().strip()
    if not raw:
        raise ValueError("no diagnosis received on stdin")
    _log(f"received stdin bytes={len(raw.encode('utf-8'))}")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("diagnosis must be a JSON object")
    keys = sorted(str(key) for key in data.keys())
    _log(f"parsed diagnosis keys={keys}")
    return data


def gh_output(*args: str) -> str:
    try:
        _log(f"running gh {' '.join(args)}")
        completed = subprocess.run(
            ["gh", *args],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError as exc:
        raise GitHubCliError("gh CLI not found") from exc
    except subprocess.CalledProcessError as exc:
        stdout = _text(exc.stdout)
        stderr = _text(exc.stderr)
        detail = stderr or stdout
        detail = " ".join(detail.split())
        command = " ".join(args)
        if detail:
            _log(
                "gh command failed "
                f"command={command!r} returncode={exc.returncode} "
                f"stdout={stdout!r} stderr={stderr!r}"
            )
            raise GitHubCliError(f"gh {command} failed: {detail}") from exc
        _log(f"gh command failed command={command!r} returncode={exc.returncode} with no output")
        raise GitHubCliError(f"gh {command} failed with status {exc.returncode}") from exc
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    _log(f"gh command succeeded args={' '.join(args)!r} stdout={stdout.strip()!r} stderr={stderr.strip()!r}")
    return stdout


def _ensure_label(label: str) -> None:
    """Create the GitHub label if it does not already exist."""
    if label in _ENSURED_LABELS:
        return
    try:
        gh_output("label", "create", label,
                  "--description", "Self-healing triggered task alerts",
                  "--color", "#e4e669")
        _ENSURED_LABELS.add(label)
    except GitHubCliError as exc:
        # "already exists" is fine; surface any other error as a warning only
        if "already exists" in str(exc).lower():
            _log(f"label already exists label={label!r}")
            _ENSURED_LABELS.add(label)
            return
        print(f"Warning: could not create label '{label}': {exc}", file=sys.stderr)


def create_issue(title: str, body: str, labels: list[str] | None = None) -> str:
    for label in (labels or []):
        _ensure_label(label)
    _log(
        f"creating issue title={title!r} labels={labels or []} "
        f"body_chars={len(body)}"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_path = Path(handle.name)
    try:
        args = ["issue", "create", "--title", title, "--body-file", str(body_path)]
        for label in (labels or []):
            args.extend(["--label", label])
        return gh_output(*args).strip()
    finally:
        body_path.unlink(missing_ok=True)


def render_issue_body(group: dict, diagnosis: str, window: str,
                      similar_issues: list[dict] | None = None) -> str:
    """Render a rich GH issue body from the agent's diagnosis."""
    task = _text(group.get("task"), "unknown")
    phase = _text(group.get("phase"), "unknown")
    message_prefix = _text(group.get("messagePrefix"), "")
    severity = _text(group.get("severity"), "unknown")
    pattern = _text(group.get("pattern"), "")
    remediation = _text(group.get("remediation"), "")
    count = group.get("count", 1)
    findings = group.get("findings", [])
    timeline = _text(group.get("correlatedTimeline"), "")
    git_context = _text(group.get("gitContext"), "")

    # Pattern marker for future de-duplication
    marker = f"<!-- self-heal task:{task} phase:{phase} prefix:{message_prefix[:40]} -->"

    lines = [
        marker,
        f"@copilot Please investigate and fix this triggered-task alert.",
        "",
        "## Summary",
        "",
        f"**Task:** `{task}` | **Phase:** `{phase}` | **Severity:** {severity} | **Count:** {count}",
        f"**Window:** {window}",
        "",
        f"### Diagnosis",
        "",
        diagnosis,
        "",
    ]

    if pattern:
        lines.extend([
            "### Identified pattern",
            "",
            pattern,
            "",
        ])

    if timeline:
        lines.extend([
            "### Correlated timeline",
            "",
            "```",
            timeline,
            "```",
            "",
        ])

    if git_context:
        lines.extend([
            "### Recent git activity",
            "",
            git_context,
            "",
        ])

    if remediation:
        lines.extend([
            "### Suggested remediation",
            "",
            remediation,
            "",
        ])

    if similar_issues:
        lines.extend(["### Similar past issues", ""])
        for issue in similar_issues:
            num = issue.get("number", "?")
            title = _text(issue.get("title"), "untitled")
            state = _text(issue.get("state"), "?")
            lines.append(f"- #{num} ({state}): {title}")
        lines.append("")

    # Raw findings for reference
    lines.extend(["## Raw findings", ""])
    for f in findings:
        if isinstance(f, dict):
            ts = _text(f.get("ts"))
            level = _text(f.get("level"))
            cat = _text(f.get("error_category"))
            msg = _text(f.get("message"))
            label = f"`{level}`"
            if cat:
                label += f" `{cat}`"
            lines.append(f"- `{ts}` — {label} — {msg}")
    lines.append("")

    # Tracebacks from findings
    tracebacks = [_text(f.get("traceback")) for f in findings
                  if isinstance(f, dict) and _text(f.get("traceback"))]
    if tracebacks:
        lines.extend(["## Tracebacks", ""])
        for tb in dict.fromkeys(tracebacks):  # dedupe, preserve order
            lines.extend(["```python", tb, "```", ""])

    # Log context
    context = group.get("context", [])
    if context:
        lines.extend([
            "## Log context (entries leading up to error)",
            "",
            "```jsonl",
        ])
        for entry in context:
            lines.append(str(entry))
        lines.extend(["```", ""])

    return "\n".join(lines) + "\n"


def main() -> int:
    data = _read_input()

    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        _log("no groups present in diagnosis payload")
        print("No actionable findings in agent diagnosis; no issues created.")
        return 0

    window = _text(data.get("window"), "unknown")
    _log(f"processing groups={len(groups)} window={window!r}")
    created = 0
    skipped_noise = 0
    skipped_invalid = 0
    failed = 0

    for index, group in enumerate(groups, start=1):
        if not isinstance(group, dict):
            skipped_invalid += 1
            _log(f"skipping non-object group index={index} type={type(group).__name__}")
            continue

        severity = _text(group.get("severity"), "unknown")
        task = _text(group.get("task"), "unknown")
        phase = _text(group.get("phase"), "unknown")
        count = group.get("count", 1)
        prefix = _text(group.get("messagePrefix"), "")[:80]
        _log(
            f"group index={index} task={task!r} phase={phase!r} severity={severity!r} "
            f"count={count!r} messagePrefix={prefix!r}"
        )
        if severity == "noise":
            skipped_noise += 1
            _log(f"skipping noise group index={index} task={task!r}")
            continue

        diagnosis = _text(group.get("diagnosis"), "No diagnosis provided.")
        similar = group.get("similarIssues")
        if not isinstance(similar, list):
            similar = []

        title = f"Self-heal: `{task}` — {_text(group.get('messagePrefix', 'error'))[:60]}"
        body = render_issue_body(group, diagnosis, window, similar)
        labels = ["self-heal"]

        try:
            url = create_issue(title, body, labels)
            print(f"Created issue for {task}: {url}")
            _log(f"created issue index={index} task={task!r} url={url!r}")
            created += 1
        except GitHubCliError as exc:
            failed += 1
            _log(f"failed to create issue index={index} task={task!r} error={str(exc)!r}")
            print(f"Failed to create issue for {task}: {exc}", file=sys.stderr)

    _log(
        "summary "
        f"created={created} skipped_noise={skipped_noise} "
        f"skipped_invalid={skipped_invalid} failed={failed}"
    )
    print(
        f"Done: {created} issue(s) created, {skipped_noise} noise group(s) skipped, "
        f"{skipped_invalid} invalid group(s) skipped, {failed} failure(s)."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (GitHubCliError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
