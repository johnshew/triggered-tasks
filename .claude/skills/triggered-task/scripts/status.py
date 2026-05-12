#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import subprocess

from headless import TriggeredTaskError, list_tasks, load_task_config, task_details

LOGS_DIR = Path("Agents/logs")


def _last_run_times(name: str) -> tuple[str, str]:
    """Return (last_ok, last_error) as '-Xd HH:MM' relative strings.

    Scans the task's log file for phase=done entries.
    Returns '-' if no matching entry found.
    """
    log_file = LOGS_DIR / f"{name}.log"
    if not log_file.is_file():
        return ("—", "—")

    last_ok_ts: str | None = None
    last_err_ts: str | None = None

    for line in log_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("phase") != "done":
            continue
        ts = entry.get("ts", "")
        status = entry.get("status", "")
        if status == "ok":
            last_ok_ts = ts
        elif status == "error":
            last_err_ts = ts

    now = datetime.now(timezone.utc)
    return (_format_ago(last_ok_ts, now), _format_ago(last_err_ts, now))


def _format_ago(ts: str | None, now: datetime) -> str:
    """Format an ISO timestamp as a human-friendly relative string.

    Examples: -1s, -4m, -1h, -59m, -2d 16h, -5d 1h
    Uses at most two significant units. Returns '—' if no timestamp.
    """
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        total_seconds = int((now - dt).total_seconds())
        if total_seconds < 0:
            total_seconds = 0
        days, rem = divmod(total_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        if days > 0:
            return f"-{days}d {hours}h" if hours else f"-{days}d"
        if hours > 0:
            return f"-{hours}h {mins}m" if mins else f"-{hours}h"
        if mins > 0:
            return f"-{mins}m"
        return f"-{secs}s"
    except (ValueError, TypeError):
        return "?"


def format_table(tasks: list[dict[str, Any]]) -> str:
    headers = ["NAME", "TYPE", "SCHEDULE/WATCH", "AGENT", "MODE", "STATE", "LAST OK", "LAST ERR"]
    rows: list[list[str]] = []
    # Pre-compute last run times per task name (only once per name)
    run_times: dict[str, tuple[str, str]] = {}
    for task in tasks:
        name = task["name"]
        if name not in run_times:
            run_times[name] = _last_run_times(name)
    for task in tasks:
        trigger_states = task.get("triggerStates", {})
        last_ok, last_err = run_times[task["name"]]
        first_row = True
        for ttype, tvalue in _trigger_lines(task):
            tstate = trigger_states.get(ttype, task["state"])
            rows.append([
                task["name"],
                ttype,
                tvalue,
                task["agent"],
                task["mode"],
                tstate,
                last_ok if first_row else "",
                last_err if first_row else "",
            ])
            first_row = False
    table = [headers, *rows]
    widths = [max(len(str(row[i])) for row in table) for i in range(len(headers))]
    return "\n".join(
        "  ".join(str(row[i]).ljust(widths[i]) for i in range(len(headers))).rstrip()
        for row in table
    )


def _trigger_lines(task: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (type_label, trigger_value) tuples — one per trigger."""
    lines: list[tuple[str, str]] = []
    if task.get("schedule"):
        lines.append(("cron", task["schedule"]))
    wp = task.get("watchPath")
    if wp:
        if isinstance(wp, list):
            for p in wp:
                lines.append(("watcher", p))
        else:
            lines.append(("watcher", wp))
    if not lines:
        lines.append((task.get("type", "?"), "-"))
    return lines


def _in_sandbox() -> bool:
    """Return True if running in a restricted sandbox where crontab is inaccessible."""
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            check=False,
        )
        return result.returncode != 0
    except FileNotFoundError:
        return False  # crontab not installed - not a sandbox issue


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="json_mode", action="store_true")
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()

    if _in_sandbox():
        print(
            "status: running in a restricted sandbox - crontab and process detection are unavailable.\n"
            "Run this command in a regular terminal for accurate task state.",
            file=sys.stderr,
        )
        return 1

    try:
        if args.name and not args.json_mode:
            raise TriggeredTaskError("--json flag is required when querying a single task")
        if args.name:
            details = task_details(load_task_config(args.name))
            print(json.dumps(details, indent=2))
            return 0

        names = list_tasks()
        if not names:
            if args.json_mode:
                print('{"tasks": []}')
            else:
                print("No tasks configured")
            return 0

        tasks = []
        for name in names:
            try:
                tasks.append(task_details(load_task_config(name)))
            except TriggeredTaskError:
                continue

        if args.json_mode:
            print(json.dumps({"tasks": tasks}, indent=2))
        else:
            print(format_table(tasks))
        return 0
    except TriggeredTaskError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
