#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Pre-processor for self-heal-log-alerts.

Scans all JSONL logs for errors/warnings, groups related findings by
(task, phase, message_prefix), checks for existing open GH issues that
already cover each group, and gates the agent run when nothing new is found.

Output:
  {"skip": true, "reason": "..."}  — no untracked findings
  {"skip": false, "groups": [...], "windowStart": "...", "windowEnd": "..."}
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_DIRS = [
    REPO_ROOT / "Agents" / "logs",
    REPO_ROOT / "Exercise" / "data" / "log",
]
QLOG_PATH = REPO_ROOT / ".claude/skills/triggered-task/scripts/qlog.py"
SELF_TASK = "self-heal-log-alerts"
ALERT_LEVELS = {"error", "warn", "warning"}
MAX_CONTEXT_ENTRIES = 10
CORRELATED_EVENT_LIMIT = 60
GIT_CONTEXT_LINE_LIMIT = 40
TIMELINE_WINDOW_PADDING_MINUTES = 5
_GIT_CONTEXT_CACHE: dict[str, list[str]] = {}
_SIMILAR_ISSUES_CACHE: dict[str, list[dict[str, object]]] = {}
_TIMELINE_CACHE: dict[tuple[str, str, str], tuple[list[str], list[str]]] = {}


def _text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _parse_ts(ts_str: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        # Ensure timezone-aware (old entries may lack tz info)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Normalize to UTC so all comparisons use the same timezone
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _message_prefix(msg: str, max_len: int = 80) -> str:
    """Normalize a message down to a stable prefix for de-duplication."""
    normalized = " ".join(msg.split())
    return normalized[:max_len]


@dataclass
class Finding:
    ts: str
    log: str
    task: str
    level: str
    phase: str
    message: str
    error_category: str = ""
    transcript_path: str = ""
    traceback: str = ""

    def to_dict(self) -> dict:
        d: dict = {
            "ts": self.ts,
            "log": self.log,
            "task": self.task,
            "level": self.level,
            "phase": self.phase,
            "message": self.message,
        }
        if self.error_category:
            d["error_category"] = self.error_category
        if self.transcript_path:
            d["transcript_path"] = self.transcript_path
        if self.traceback:
            d["traceback"] = self.traceback
        return d


@dataclass
class FindingGroup:
    """Related findings grouped by (task, phase, message_prefix)."""
    task: str
    phase: str
    message_prefix: str
    findings: list[Finding] = field(default_factory=list)
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def group_key(self) -> tuple[str, str, str]:
        return (self.task, self.phase, self.message_prefix)

    def to_dict(self) -> dict:
        payload = {
            "task": self.task,
            "phase": self.phase,
            "messagePrefix": self.message_prefix,
            "count": len(self.findings),
            "findings": [f.to_dict() for f in self.findings],
            "context": gather_log_context(self.findings[0]) if self.findings else [],
        }
        payload.update(self.evidence)
        return payload


def _run_command(command: list[str], *, timeout: int = 30) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout or "").strip()


def _sql_quote(value: str) -> str:
    return value.replace("'", "''")


def _format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _group_window(group: FindingGroup) -> tuple[str, str]:
    timestamps = [_parse_ts(f.ts) for f in group.findings]
    resolved = [ts for ts in timestamps if ts is not None]
    if not resolved:
        now = datetime.now(timezone.utc)
        return _format_ts(now - timedelta(minutes=TIMELINE_WINDOW_PADDING_MINUTES)), _format_ts(now)
    window_start = min(resolved) - timedelta(minutes=TIMELINE_WINDOW_PADDING_MINUTES)
    window_end = max(resolved) + timedelta(minutes=TIMELINE_WINDOW_PADDING_MINUTES)
    return _format_ts(window_start), _format_ts(window_end)


def _summarize_qlog_rows(raw_output: str) -> list[str]:
    lines: list[str] = []
    for raw_line in raw_output.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        ts = _text(entry.get("ts"))
        src = _text(entry.get("_src"))
        task = _text(entry.get("task"))
        phase = _text(entry.get("phase"))
        status = _text(entry.get("status"))
        trigger = _text(entry.get("trigger"))
        duration = entry.get("duration_s")
        message = _text(entry.get("message"))
        parts = [part for part in [task or src, phase, status] if part]
        if trigger:
            parts.append(f"trigger={trigger}")
        if duration not in (None, ""):
            parts.append(f"duration_s={duration}")
        if message:
            parts.append(f"message={message[:120]}")
        lines.append(f"{ts} {' '.join(parts)}".strip())
    return lines


def gather_correlated_timeline(group: FindingGroup) -> tuple[str, str, list[str], list[str]]:
    window_start, window_end = _group_window(group)
    cache_key = (group.task, window_start, window_end)
    cached = _TIMELINE_CACHE.get(cache_key)
    if cached is not None:
        correlated, slow_runs = cached
        return window_start, window_end, correlated, slow_runs

    task_name = _sql_quote(group.task)
    base_sql = (
        "SELECT ts,_src,task,phase,status,trigger,duration_s,message "
        "FROM log "
        f"WHERE ts >= '{window_start}' AND ts < '{window_end}' "
        f"AND task = '{task_name}' "
        "ORDER BY ts "
        f"LIMIT {CORRELATED_EVENT_LIMIT}"
    )
    slow_sql = (
        "SELECT ts,_src,task,phase,status,trigger,duration_s,message "
        "FROM log "
        f"WHERE ts >= '{window_start}' AND ts < '{window_end}' "
        f"AND task = '{task_name}' AND duration_s > 30 "
        "ORDER BY ts "
        f"LIMIT {CORRELATED_EVENT_LIMIT}"
    )
    correlated = _summarize_qlog_rows(
        _run_command(
            [str(QLOG_PATH), "--all", "--sql", base_sql, "--format", "jsonl"],
            timeout=45,
        )
    )
    slow_runs = _summarize_qlog_rows(
        _run_command(
            [str(QLOG_PATH), "--all", "--sql", slow_sql, "--format", "jsonl"],
            timeout=45,
        )
    )
    _TIMELINE_CACHE[cache_key] = (correlated, slow_runs)
    return window_start, window_end, correlated, slow_runs


def gather_git_context(window_start: str) -> list[str]:
    cached = _GIT_CONTEXT_CACHE.get(window_start)
    if cached is not None:
        return cached
    since_dt = _parse_ts(window_start)
    if since_dt is None:
        return []
    history_start = since_dt - timedelta(hours=3)
    output = _run_command(
        [
            "git",
            "--no-pager",
            "log",
            "--oneline",
            "--stat",
            f"--since={history_start.strftime('%Y-%m-%d %H:%M:%SZ')}",
            "--",
            "Agents/",
            "Exercise/",
            "To Do/",
        ],
        timeout=30,
    )
    lines = output.splitlines()[:GIT_CONTEXT_LINE_LIMIT] if output else []
    _GIT_CONTEXT_CACHE[window_start] = lines
    return lines


def gather_similar_issues(task_name: str) -> list[dict[str, object]]:
    cached = _SIMILAR_ISSUES_CACHE.get(task_name)
    if cached is not None:
        return cached
    output = _run_command(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "all",
            "--limit",
            "20",
            "--label",
            "self-heal",
            "--search",
            task_name,
            "--json",
            "number,title,state,url",
        ],
        timeout=20,
    )
    if not output:
        _SIMILAR_ISSUES_CACHE[task_name] = []
        return []
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        _SIMILAR_ISSUES_CACHE[task_name] = []
        return []
    issues = parsed if isinstance(parsed, list) else []
    _SIMILAR_ISSUES_CACHE[task_name] = issues
    return issues


MAX_RUN_OUTPUT_CHARS = 4000
RUNS_DIR = REPO_ROOT / "Agents" / "logs" / "runs"


def gather_run_output(group: FindingGroup) -> list[dict[str, str]]:
    """Find run output files for this task near the error window.

    Returns a list of dicts with keys: file, content (truncated if long).
    """
    if not RUNS_DIR.is_dir():
        return []
    window_start, window_end = _group_window(group)
    ws_dt = _parse_ts(window_start)
    we_dt = _parse_ts(window_end)
    if not ws_dt or not we_dt:
        return []
    # Widen window slightly to catch runs that started before the error
    ws_dt = ws_dt - timedelta(minutes=10)
    we_dt = we_dt + timedelta(minutes=5)

    prefix = f"{group.task}-"
    # Accepted compound extensions: .stdout.txt, .stderr.txt, .transcript.md
    ACCEPTED_SUFFIXES = (".stdout.txt", ".stderr.txt", ".transcript.md")
    results: list[dict[str, str]] = []
    for path in sorted(RUNS_DIR.iterdir()):
        if not path.name.startswith(prefix):
            continue
        if not any(path.name.endswith(s) for s in ACCEPTED_SUFFIXES):
            continue
        # Identity portion is everything before the first '.'
        # e.g. "task-20260505T120000Z-timeout" from "task-20260505T120000Z-timeout.stdout.txt"
        base = path.name.split(".", 1)[0]
        name_after_prefix = base[len(prefix):]
        ts_part = name_after_prefix.split("-")[0] if "-" in name_after_prefix else name_after_prefix
        if len(ts_part) >= 15 and ts_part.endswith("Z"):
            try:
                file_dt = datetime.strptime(ts_part, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if file_dt < ws_dt or file_dt > we_dt:
                continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if content == "(empty)":
            continue
        # Summarize if very long
        if len(content) > MAX_RUN_OUTPUT_CHARS:
            head = content[:MAX_RUN_OUTPUT_CHARS // 2]
            tail = content[-(MAX_RUN_OUTPUT_CHARS // 2):]
            content = f"{head}\n\n... ({len(content)} chars total, middle truncated) ...\n\n{tail}"
        results.append({"file": path.name, "content": content})
    return results


def enrich_groups(groups: list[FindingGroup]) -> list[FindingGroup]:
    for group in groups:
        window_start, window_end, correlated, slow_runs = gather_correlated_timeline(group)
        group.evidence = {
            "windowStart": window_start,
            "windowEnd": window_end,
            "correlatedTimeline": correlated,
            "slowRuns": slow_runs,
            "gitContext": gather_git_context(window_start),
            "similarIssues": gather_similar_issues(group.task),
            "runOutput": gather_run_output(group),
        }
    return groups


def scan_logs(since: datetime) -> list[Finding]:
    """Read all JSONL logs and return findings from the window."""
    # Ensure since is timezone-aware UTC to prevent naive/aware comparison crashes
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    else:
        since = since.astimezone(timezone.utc)

    seen: set[tuple[str, str, str, str]] = set()
    findings: list[Finding] = []

    for log_dir in LOG_DIRS:
        for log_path in sorted(log_dir.glob("*.log")):
            task_name = log_path.stem
            if task_name == SELF_TASK:
                continue
            rel_path = str(log_path)
            try:
                lines = log_path.read_text().splitlines()
            except OSError:
                continue
            entries: list[dict[str, object]] = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
            for entry in entries:

                ts_str = _text(entry.get("ts"))
                dt = _parse_ts(ts_str)
                try:
                    if not dt or dt < since:
                        continue
                except TypeError:
                    # Skip entries whose timestamps can't be compared
                    # (e.g. residual naive/aware mismatch despite normalization)
                    continue

                level = _text(entry.get("level")).lower()
                status = _text(entry.get("status")).lower()

                is_alert = level in ALERT_LEVELS or status == "error"
                if not is_alert:
                    continue

                task = _text(entry.get("task")) or task_name
                phase = _text(entry.get("phase"), "unknown")
                message = " ".join(_text(entry.get("message"), "(no message)").split())
                error_category = _text(entry.get("error_category"))
                transcript_path = _text(entry.get("transcript_path"))
                traceback_text = _text(entry.get("traceback"))

                dedupe_key = (ts_str, task, phase, message)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)

                findings.append(Finding(
                    ts=ts_str, log=rel_path, task=task, level=level,
                    phase=phase, message=message,
                    error_category=error_category,
                    transcript_path=transcript_path,
                    traceback=traceback_text,
                ))

    return findings


def gather_log_context(finding: Finding) -> list[str]:
    """Return recent JSONL lines leading up to the error."""
    log_path = Path(finding.log)
    if not log_path.is_file():
        return []
    finding_dt = _parse_ts(finding.ts)
    if not finding_dt:
        return []
    window_start = finding_dt - timedelta(hours=1)
    relevant: list[str] = []
    try:
        lines = log_path.read_text().splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        ts_str = _text(entry.get("ts"))
        dt = _parse_ts(ts_str)
        try:
            if not dt or dt < window_start or dt > finding_dt:
                continue
        except TypeError:
            # Skip entries whose timestamps can't be compared
            continue
        relevant.append(line)
    return relevant[-MAX_CONTEXT_ENTRIES:]


def group_findings(findings: list[Finding]) -> list[FindingGroup]:
    """Group findings by (task, phase, error_category or message_prefix)."""
    groups: dict[tuple[str, str, str], FindingGroup] = {}
    for f in findings:
        # Prefer error_category when available — it's more stable than message text
        prefix = f.error_category if f.error_category else _message_prefix(f.message)
        key = (f.task, f.phase, prefix)
        if key not in groups:
            groups[key] = FindingGroup(task=f.task, phase=f.phase, message_prefix=prefix)
        groups[key].findings.append(f)
    return list(groups.values())


def check_existing_open_issues(groups: list[FindingGroup]) -> list[FindingGroup]:
    """Filter out groups that already have a matching open GH issue.

    Matches by (task, phase, message_prefix) in the issue body, not by
    exact timestamp. This prevents re-filing the same recurring error.
    """
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--state", "open", "--limit", "100",
             "--label", "self-heal", "--json", "number,title,body,url"],
            check=True, capture_output=True, text=True,
        )
        open_issues = json.loads(result.stdout or "[]")
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
        # If we can't check issues, let everything through — the agent
        # will do a more thorough check.
        return groups

    if not isinstance(open_issues, list):
        return groups

    remaining: list[FindingGroup] = []
    for group in groups:
        marker = f"task:{group.task} phase:{group.phase}"
        already_tracked = False
        for issue in open_issues:
            if not isinstance(issue, dict):
                continue
            body = _text(issue.get("body"))
            title = _text(issue.get("title"))
            if marker in body or group.task in title:
                # Also check message prefix for tighter matching
                if group.message_prefix[:40] in body:
                    already_tracked = True
                    break
        if not already_tracked:
            remaining.append(group)
    return remaining


def main() -> int:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=1)

    findings = scan_logs(since)
    if not findings:
        print(json.dumps({"skip": True, "reason": "No error/warning entries in the last hour."}))
        return 0

    groups = group_findings(findings)
    untracked = check_existing_open_issues(groups)
    enriched = enrich_groups(untracked)

    if not enriched:
        print(json.dumps({
            "skip": True,
            "reason": f"Found {len(findings)} alert(s) in {len(groups)} group(s), but all already have matching open issues.",
        }))
        return 0

    print(json.dumps({
        "skip": False,
        "windowStart": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "windowEnd": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totalFindings": len(findings),
        "totalGroups": len(groups),
        "untrackedGroups": len(enriched),
        "groups": [g.to_dict() for g in enriched],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
