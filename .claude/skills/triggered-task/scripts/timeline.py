#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""timeline - correlated view of triggered-task processing events.

Reads all JSONL logs in Agents/logs/ (and optionally Exercise/data/log/),
merges adjacent info+status entries into single readable lines, and
optionally interleaves systemd journal entries to show timer/spawn lifecycle.

Works for any triggered task: exercise, taskflow, flagged-email, todo, etc.

Usage:
    uv run --script .claude/skills/triggered-task/scripts/timeline.py exercise-state-update --since 2026-05-01T12:00
    uv run --script .claude/skills/triggered-task/scripts/timeline.py LMCO --systemd
    uv run --script .claude/skills/triggered-task/scripts/timeline.py --all --since 2026-05-01T16:00
    uv run --script .claude/skills/triggered-task/scripts/timeline.py flagged-email --last 30
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]  # .claude/skills/triggered-task/scripts -> life/
LOGS_DIR = REPO / "Agents" / "logs"
EXERCISE_LOGS = REPO / "Exercise" / "data" / "log"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Correlated timeline of triggered-task processing events",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  timeline.py exercise-state-update --since 2026-05-01T12:00
  timeline.py LMCO --systemd --since 2026-05-01T16:48:00
  timeline.py --all --since 2026-05-01T16:00 --last 100
  timeline.py flagged-email-triage --last 20""",
    )
    p.add_argument("filter", nargs="?", help="Task name or content substring (case-insensitive)")
    p.add_argument("--all", action="store_true", help="Show all tasks (no filter)")
    p.add_argument("--since", help="Start time (ISO-8601 UTC)")
    p.add_argument("--last", type=int, default=50, help="Last N events (default 50)")
    p.add_argument("--systemd", action="store_true", help="Include systemd journal entries (timers, scopes)")
    p.add_argument("--logs", nargs="*", help="Specific log files to read (default: all in Agents/logs/)")
    return p.parse_args()


def find_log_files(specific: list[str] | None) -> list[Path]:
    """Find all relevant JSONL log files."""
    if specific:
        return [Path(f) for f in specific if Path(f).exists()]
    files = sorted(LOGS_DIR.glob("*.log")) if LOGS_DIR.exists() else []
    if EXERCISE_LOGS.exists():
        files.extend(sorted(EXERCISE_LOGS.glob("*.log")))
    return files


def load_jsonl(path: Path, text_filter: str | None, since: str | None) -> list[dict]:
    """Load JSONL log, filtering by content substring and time.

    Infers missing 'task' fields from surrounding entries (info-level
    entries emitted by the triggered-task runner sometimes omit 'task').
    """
    entries = []
    if not path.exists():
        return entries
    last_task = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Track task context for entries that omit it
            if d.get("task"):
                last_task = d["task"]
            elif last_task:
                d["task"] = last_task
            if since and d.get("ts", "") < since:
                continue
            if text_filter and text_filter.lower() not in json.dumps(d).lower():
                continue
            entries.append(d)
    return entries


def extract_message(entry: dict) -> str:
    """Extract the most useful human-readable message from a log entry."""
    msg = entry.get("message", "")
    if msg:
        # Collapse multi-line into semicolons, strip common handler prefixes
        lines = [l.strip() for l in msg.split("\n") if l.strip()]
        lines = [re.sub(r"^\[[\w-]+\]\s*", "", l) for l in lines]
        lines = [l for l in lines if l]
        return "; ".join(lines)

    if entry.get("summary"):
        return entry["summary"]
    if entry.get("output") and len(entry["output"]) < 200:
        return entry["output"][:150]
    return ""


def format_duration(d: float | None) -> str:
    if d is None:
        return ""
    if d < 1:
        return f"{d*1000:.0f}ms"
    if d < 60:
        return f"{d:.1f}s"
    return f"{d/60:.1f}m"


def phase_icon(phase: str, status: str | None) -> str:
    """Text label for phase/status."""
    if status == "error":
        return "FAIL"
    if status == "skipped":
        return "SKIP"
    icons = {
        "watcher": "WATCH",
        "start": "START",
        "pre-processor": "PREP",
        "agent": "AGENT",
        "post-processor": "POST",
        "done": "DONE",
        "handler": "APPLY",
        "activate": "ACTIV",
    }
    return icons.get(phase, phase.upper()[:5])


def get_systemd_entries(text_filter: str | None, since: str | None) -> list[dict]:
    """Get relevant systemd journal entries for timers, scopes, and spawns.

    Log files use UTC timestamps. journalctl --since interprets values as
    local time. We convert the UTC since to local for the query, then
    convert journal entry timestamps back to UTC for proper interleaving.
    """
    cmd = ["journalctl", "--user", "--no-pager", "--output=short-iso"]
    if since:
        try:
            utc_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
            local_dt = utc_dt.astimezone()
            cmd += ["--since", local_dt.strftime("%Y-%m-%d %H:%M:%S")]
        except (ValueError, OSError):
            cmd += ["--since", since.replace("T", " ")[:19]]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    entries = []
    for line in result.stdout.splitlines():
        lower = line.lower()
        # Include timer, scope, and spawn-related lines
        if not any(kw in lower for kw in ("flush", "spawn", "debounce", "scope")):
            continue
        if text_filter and text_filter.lower() not in lower:
            continue
        # Parse: "2026-05-01T12:50:48-04:00 hostname unit[pid]: message"
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-]\d{2}:?\d{2})?\s+\S+\s+(.*)", line)
        if m:
            ts_local = m.group(1)
            tz_offset = m.group(2)
            msg = m.group(3).strip()
            # Convert to UTC
            if tz_offset:
                clean_offset = tz_offset.replace(":", "")
                sign = 1 if clean_offset[0] == "+" else -1
                hours = int(clean_offset[1:3])
                mins = int(clean_offset[3:5])
                offset_seconds = sign * (hours * 3600 + mins * 60)
                try:
                    local_dt = datetime.fromisoformat(ts_local)
                    utc_dt = local_dt - timedelta(seconds=offset_seconds)
                    ts_utc = utc_dt.strftime("%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    ts_utc = ts_local
            else:
                ts_utc = ts_local
            entries.append({
                "ts": ts_utc,
                "source": "systemd",
                "message": msg,
            })
    return entries


def print_timeline(events: list[dict]) -> None:
    """Print a formatted timeline.

    Merges phase info-level detail entries with their corresponding
    status entries for a compact one-line-per-event view.
    """
    if not events:
        print("  (no events found)")
        return

    # Merge: info-level entries (message detail) with their adjacent
    # status entries (ok/error + duration).
    merged = []
    i = 0
    while i < len(events):
        ev = events[i]
        if ev.get("source") == "systemd":
            merged.append(ev)
            i += 1
            continue

        # Is this an info-only entry (has message, no status)?
        if ev.get("message") and not ev.get("status"):
            matched = False
            for j in (i + 1, i - 1):
                if 0 <= j < len(events):
                    other = events[j]
                    if (other.get("ts") == ev.get("ts")
                            and other.get("task") == ev.get("task")
                            and other.get("phase") == ev.get("phase")
                            and other.get("status")):
                        other.setdefault("_detail", "")
                        other["_detail"] = extract_message(ev)
                        matched = True
                        break
            if not matched:
                merged.append(ev)
            i += 1
            continue

        merged.append(ev)
        i += 1

    # Print
    print()
    prev_task = None
    for ev in merged:
        source = ev.get("source", "log")
        ts = ev.get("ts", "")[:19]
        ts_display = ts.replace("T", " ")

        if source == "systemd":
            msg = ev["message"]
            # Shorten paths for readability
            msg = re.sub(r"/home/\w+/\.local/bin/", "", msg)
            msg = re.sub(r"/home/\w+/repos/\w+/", "", msg)
            msg = re.sub(r"\.claude/skills/triggered-task/scripts/", "", msg)
            msg = re.sub(r"Taskflow/agents/handlers/", "", msg)
            msg = re.sub(r"Agents/handlers/", "", msg)
            msg = msg.replace(" - ", ": ", 1) if ": " not in msg else msg
            print(f"  {ts_display}  {'SYSD':<6} {msg[:120]}")
            continue

        task = ev.get("task", "?")
        phase = ev.get("phase", "?")
        status = ev.get("status", "")
        dur = ev.get("duration_s")
        changed = ev.get("changed_files", [])
        icon = phase_icon(phase, status)

        # Visual separator on task change
        if task != prev_task and prev_task is not None:
            print()
        prev_task = task

        # Build detail
        parts = []

        if status and status not in ("start", "ok", "error", "skipped"):
            parts.append(f"[{status}]")

        if dur is not None:
            parts.append(f"({format_duration(dur)})")

        if changed:
            files_str = ", ".join(Path(f).name for f in changed[:3])
            if len(changed) > 3:
                files_str += f" +{len(changed)-3}"
            parts.append(f"files=[{files_str}]")

        detail_msg = ev.get("_detail") or extract_message(ev)
        if detail_msg:
            parts.append(detail_msg)

        if ev.get("tokens_in"):
            parts.append(f"tokens={ev['tokens_in']}in/{ev['tokens_out']}out")
        if ev.get("premium_requests"):
            parts.append(f"reqs={ev['premium_requests']}")

        if phase == "done" and ev.get("summary") and ev["summary"] not in (detail_msg or ""):
            parts.append(ev["summary"])

        detail = " ".join(parts)
        max_detail = 130
        if len(detail) > max_detail:
            detail = detail[:max_detail] + "..."

        print(f"  {ts_display}  {icon:<6} {task:<20} {detail}")


def main() -> None:
    args = parse_args()
    if not args.filter and not args.all:
        print("Error: specify a filter string or --all", file=sys.stderr)
        sys.exit(1)

    text_filter = args.filter if not args.all else None
    log_files = find_log_files(args.logs)

    # Load all matching entries from all log files
    all_entries: list[dict] = []
    for log_file in log_files:
        all_entries.extend(load_jsonl(log_file, text_filter, args.since))

    # Sort by timestamp
    all_entries.sort(key=lambda e: e.get("ts", ""))

    # Trim to last N
    if len(all_entries) > args.last:
        all_entries = all_entries[-args.last:]

    # Add systemd entries if requested
    if args.systemd:
        systemd_entries = get_systemd_entries(text_filter, args.since)
        all_entries = all_entries + systemd_entries
        all_entries.sort(key=lambda e: e.get("ts", ""))

    # Print header
    filter_desc = f"filter={args.filter}" if args.filter else "all tasks"
    since_desc = f" since {args.since}" if args.since else ""
    print(f"\nTimeline ({filter_desc}{since_desc}, last {args.last})")
    print("=" * 80)

    # Deduplicate and skip redundant sub-phase start entries
    seen = set()
    deduped = []
    for ev in all_entries:
        if ev.get("source") == "systemd":
            deduped.append(ev)
            continue
        phase = ev.get("phase", "")
        status = ev.get("status", "")
        key = (ev.get("ts", ""), ev.get("task", ""), phase, status)
        if key in seen:
            continue
        seen.add(key)
        if status == "start" and phase in ("pre-processor", "agent", "post-processor"):
            continue
        deduped.append(ev)

    print_timeline(deduped)

    # Summary stats
    done_entries = [e for e in deduped if e.get("phase") == "done" and e.get("source") != "systemd"]
    if done_entries:
        print(f"\n{'─' * 80}")
        ok = sum(1 for e in done_entries if e.get("status") == "ok")
        err = sum(1 for e in done_entries if e.get("status") == "error")
        skip = sum(1 for e in done_entries if e.get("status") == "skipped")
        total_dur = sum(e.get("duration_s", 0) for e in done_entries if e.get("duration_s"))
        print(f"  Runs: {len(done_entries)} ({ok} ok, {err} error, {skip} skipped) | Total time: {format_duration(total_dur)}")
    print()


if __name__ == "__main__":
    main()
