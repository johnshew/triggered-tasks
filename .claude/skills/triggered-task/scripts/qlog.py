#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["duckdb>=1.0"]
# ///
"""Query the JSONL task logs with DuckDB.

Examples
--------
Events for one task in a time window:
    qlog.py --task exercise-state-update --since 2026-04-22T13:00 --until 2026-04-22T13:30

Slow runs (duration > 30s) in the last day:
    qlog.py --slow 30 --since 2026-04-21

Errors across all logs merged:
    qlog.py --errors --all

Correlated view across all logs in a window (for race diagnosis):
    qlog.py --all --since 2026-04-22T13:02 --until 2026-04-22T13:10

Custom SQL:
    qlog.py --sql "SELECT task, COUNT(*) FROM log GROUP BY task ORDER BY 2 DESC"
"""
from __future__ import annotations

import argparse
import glob as _glob
import sys
from pathlib import Path

import duckdb

import re
from datetime import datetime, timedelta, timezone

REPO = Path(__file__).resolve().parents[4]  # life/
DEFAULT_LOG = REPO / "Agents/logs/triggered-tasks.log"
ARCHIVE_DIR = REPO / "Agents/logs/archive"
ALL_LOG_GLOBS = [
    str(REPO / "Agents/logs/*.log"),
    str(REPO / "Exercise/data/log/*.log"),
]


def _expand(patterns: list[str]) -> list[str]:
    files: list[str] = []
    for p in patterns:
        files.extend(_glob.glob(p) if any(c in p for c in "*?[") else [p])
    return sorted(f for f in files if Path(f).is_file())


def build_view(con: duckdb.DuckDBPyConnection, patterns: list[str]) -> None:
    """Create a `log` view over the given files plus Parquet archives.

    Each file is read separately (read_json_auto infers schema per file)
    and the results are unioned by name so schema drift across files
    doesn't collapse rows into a raw JSON column.

    Adds `_src` (filename) for provenance.
    """
    files = _expand(patterns)
    if not files:
        raise SystemExit(f"no log files matched: {patterns}")
    selects = []
    for f in files:
        selects.append(
            f"SELECT *, '{Path(f).name}' AS _src "
            f"FROM read_json_auto('{f}', format='newline_delimited', "
            f"ignore_errors=true, maximum_object_size=16777216)"
        )
    # Include Parquet archive files if any exist
    # Supports both unified (archive/YYYY-MM.parquet) and legacy per-task
    # (archive/<task>/YYYY-MM.parquet) layouts.
    if ARCHIVE_DIR.is_dir():
        # Unified monthly files at archive root
        unified_files = sorted(ARCHIVE_DIR.glob("*.parquet"))
        if unified_files:
            paths_csv = ", ".join(f"'{p}'" for p in unified_files)
            selects.append(
                f"SELECT * "
                f"FROM read_parquet([{paths_csv}], union_by_name=true)"
            )
        # Legacy per-task subdirectories
        for subdir in sorted(ARCHIVE_DIR.iterdir()):
            if not subdir.is_dir():
                continue
            parquet_files = sorted(subdir.glob("*.parquet"))
            if parquet_files:
                paths_csv = ", ".join(f"'{p}'" for p in parquet_files)
                selects.append(
                    f"SELECT *, '{subdir.name}' AS _src "
                    f"FROM read_parquet([{paths_csv}], union_by_name=true)"
                )
    union = " UNION ALL BY NAME ".join(selects)
    # Create the base view, then wrap it with a computed _files column
    # that renders changed_files as a compact comma-separated basename list.
    con.sql(f"CREATE VIEW _log_raw AS {union}")
    con.sql("""
        CREATE VIEW log AS
        SELECT *,
               CASE WHEN changed_files IS NOT NULL
                    THEN array_to_string(
                           list_transform(changed_files,
                             x -> regexp_replace(x, '^.*/', '')),
                           ', ')
                    ELSE NULL
               END AS _files
        FROM _log_raw
    """)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=str(DEFAULT_LOG),
                    help=f"log file or glob (default: {DEFAULT_LOG.relative_to(REPO)})")
    ap.add_argument("--all", action="store_true",
                    help="union all Agents/logs/*.log + Exercise/data/log/*.log")
    ap.add_argument("--task", help="filter by task name (substring match)")
    ap.add_argument("--since", help="ts >= this (ISO-8601, UTC)")
    ap.add_argument("--until", help="ts < this (ISO-8601, UTC)")
    ap.add_argument("--phase", help="filter by phase (start|done|watcher|activate|...)")
    ap.add_argument("--status", help="filter by status (ok|error|skipped|...)")
    ap.add_argument("--trigger", help="filter by trigger (cron|file-change|...)")
    ap.add_argument("--slow", type=float, metavar="SEC",
                    help="only runs with duration_s > SEC")
    ap.add_argument("--errors", action="store_true",
                    help="only level=error OR status=error")
    ap.add_argument("--limit", type=int, default=200, help="max rows (default 200; 0=unlimited)")
    ap.add_argument("--columns", default="ts,_src,task,phase,status,trigger,duration_s,message",
                    help="comma-separated columns to show")
    ap.add_argument("--order", default="ts", help="ORDER BY clause (default: ts)")
    ap.add_argument("--sql", help="run custom SQL against the `log` view (ignores other filters)")
    ap.add_argument("--format", choices=["table", "jsonl", "csv"], default="table")
    args = ap.parse_args()

    con = duckdb.connect(":memory:")
    build_view(con, ALL_LOG_GLOBS if args.all else [args.log])

    # Resolve relative time specs (e.g. "1h", "2d", "30m") to ISO timestamps
    _RELATIVE_RE = re.compile(r"^(\d+)([mhd])$")
    def _resolve_ts(val: str | None) -> str | None:
        if val is None:
            return None
        m = _RELATIVE_RE.match(val)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
            return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
        return val

    if args.sql:
        # Warn if the user mixed filter flags with --sql; those filters are
        # ignored because --sql is the exact query. This trips people up
        # (including me) — produce deterministically-wrong counts if silent.
        ignored = [
            name for name, val in (
                ("--task", args.task), ("--since", args.since),
                ("--until", args.until), ("--phase", args.phase),
                ("--status", args.status), ("--trigger", args.trigger),
                ("--slow", args.slow), ("--errors", args.errors),
            ) if val
        ]
        if ignored:
            print(f"warning: --sql ignores filter flags: {', '.join(ignored)}. "
                  f"Include the conditions in your SQL WHERE clause.",
                  file=sys.stderr)
        q = args.sql
    else:
        where = []
        if args.task:    where.append(f"task LIKE '%{args.task}%'")
        if args.since:   where.append(f"ts >= '{_resolve_ts(args.since)}'")
        if args.until:   where.append(f"ts < '{_resolve_ts(args.until)}'")
        if args.phase:   where.append(f"phase = '{args.phase}'")
        if args.status:  where.append(f"status = '{args.status}'")
        if args.trigger: where.append(f"trigger = '{args.trigger}'")
        if args.slow:    where.append(f"duration_s > {args.slow}")
        if args.errors:  where.append("(level='error' OR status='error')")
        wsql = "WHERE " + " AND ".join(where) if where else ""
        # When showing errors, inject error_category after status if not already present
        col_list = args.columns.split(",")
        if args.errors and "error_category" not in col_list:
            idx = col_list.index("status") + 1 if "status" in col_list else len(col_list)
            col_list.insert(idx, "error_category")
        cols = ", ".join(col_list)
        lim = f"LIMIT {args.limit}" if args.limit else ""
        q = f"SELECT {cols} FROM log {wsql} ORDER BY {args.order} {lim}"

    try:
        rel = con.sql(q)
    except duckdb.Error as e:
        print(f"query error: {e}", file=sys.stderr)
        print(f"  sql: {q}", file=sys.stderr)
        return 2

    if args.format == "jsonl":
        import json
        cols = rel.columns
        for row in rel.fetchall():
            print(json.dumps(dict(zip(cols, row, strict=True)), default=str))
    elif args.format == "csv":
        print(",".join(rel.columns))
        for row in rel.fetchall():
            print(",".join("" if v is None else str(v).replace(",", ";") for v in row))
    else:
        # Widen display so columns aren't hidden or truncated.
        # max_width=500 lets the table exceed terminal width (wraps naturally).
        # max_col_width=80 keeps individual columns readable.
        rel.show(max_col_width=80, max_width=500)

    # When --errors is active and format is table, show tracebacks separately
    if args.errors and args.format == "table" and not args.sql:
        try:
            tb_rel = con.sql(
                f"SELECT ts, task, traceback "
                f"FROM log {wsql} AND traceback IS NOT NULL "
                f"ORDER BY ts {lim}"
            )
            rows = tb_rel.fetchall()
            if rows:
                print("\n── Tracebacks ──")
                for ts, task, tb in rows:
                    # Show last 20 lines of traceback
                    lines = tb.strip().splitlines()
                    tail = "\n".join(lines[-20:])
                    print(f"\n[{ts}] {task}:")
                    print(tail)
        except duckdb.Error:
            pass  # traceback column may not exist in older logs

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
