#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from headless import logs_root, system_log


def read_log(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").strip().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def summarize_message(entry: dict, max_len: int = 60) -> str:
    phase = entry.get("phase", "")
    duration = entry.get("duration_s")
    prefix = f"{duration}s " if duration is not None else ""

    if phase == "start":
        parts = []
        if entry.get("trigger"):
            parts.append(f"trigger={entry['trigger']}")
        if entry.get("agent"):
            parts.append(f"agent={entry['agent']}")
        if entry.get("mode"):
            parts.append(f"mode={entry['mode']}")
        return prefix + " ".join(parts)

    if phase == "done":
        parts = []
        msg = entry.get("message", "")
        if msg:
            parts.append(msg[:max_len])
        return prefix + " ".join(parts)

    if phase == "activate":
        parts = []
        if entry.get("type"):
            parts.append(entry["type"])
        if entry.get("schedule"):
            parts.append(entry["schedule"])
        if entry.get("watchPath"):
            parts.append(entry["watchPath"])
        return prefix + " ".join(parts)

    # agent / handler phases
    output = entry.get("output", "")
    message = entry.get("message", "")
    text = output or message
    if not text:
        return prefix.rstrip()
    # Collapse newlines for table display
    flat = text.replace("\n", " ").replace("\r", "")
    if len(flat) > max_len:
        return prefix + flat[:max_len - 3] + "..."
    return prefix + flat


def format_table(entries: list[dict], show_task: bool) -> str:
    if not entries:
        return "(no entries)"

    rows: list[list[str]] = []
    for entry in entries:
        ts = entry.get("ts", "")
        time_str = ts[11:19] if len(ts) >= 19 else ts
        level = entry.get("level", "")
        phase = entry.get("phase", "")
        status = entry.get("status", "")
        message = summarize_message(entry)

        if show_task:
            task = entry.get("task", "")
            rows.append([time_str, level, task, phase, status, message])
        else:
            rows.append([time_str, level, phase, status, message])

    if show_task:
        headers = ["TIME", "LEVEL", "TASK", "PHASE", "STATUS", "MESSAGE"]
    else:
        headers = ["TIME", "LEVEL", "PHASE", "STATUS", "MESSAGE"]

    # Compute column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)).rstrip()

    lines = [fmt_row(headers)]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(fmt_row(row))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="View triggered-task logs")
    parser.add_argument("name", nargs="?", help="Task name (omit for system log)")
    parser.add_argument("--all", action="store_true", help="Merge all task logs by timestamp")
    parser.add_argument("--errors", action="store_true", help="Show errors only")
    parser.add_argument("-n", type=int, default=20, help="Number of entries (default: 20)")
    parser.add_argument("--json", action="store_true", dest="raw_json", help="Raw JSONL output")
    args = parser.parse_args()

    if args.all:
        # Merge all task logs + system log
        entries: list[dict] = []
        for log_file in sorted(logs_root().glob("*.log")):
            task_name = log_file.stem
            for entry in read_log(log_file):
                if "task" not in entry:
                    entry["task"] = task_name
                entries.append(entry)
        entries.sort(key=lambda e: e.get("ts", ""))
        show_task = True
    elif args.name:
        log_path = logs_root() / f"{args.name}.log"
        if not log_path.is_file():
            print(f"No log file found: {args.name}.log", file=sys.stderr)
            return 1
        entries = read_log(log_path)
        show_task = False
    else:
        # System log
        entries = read_log(system_log())
        if not entries:
            print("No system log entries yet.", file=sys.stderr)
            return 1
        show_task = True

    if args.errors:
        entries = [e for e in entries if e.get("level") == "error" or e.get("status") == "error"]

    # Take last N entries
    entries = entries[-args.n:]

    if args.raw_json:
        for entry in entries:
            print(json.dumps(entry, ensure_ascii=False))
    else:
        print(format_table(entries, show_task=show_task))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
