#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
from __future__ import annotations

import argparse
import sys

from headless import (
    TriggeredTaskError,
    load_task_config,
    remove_cron_entries,
    remove_desired_cron,
    remove_desired_watcher,
    stop_watcher,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    try:
        task_type = ""
        try:
            task_type = load_task_config(args.name).task_type
        except TriggeredTaskError:
            task_type = ""

        if task_type in {"", "cron", "multi"}:
            removed = remove_cron_entries(args.name)
            remove_desired_cron(args.name)
            if removed:
                print(f"Removed cron entry for '{args.name}'")
            elif task_type == "cron":
                print(f"No cron entry found for '{args.name}' (may not have been started)")

        if task_type in {"", "watcher", "multi"}:
            pid = stop_watcher(args.name)
            remove_desired_watcher(args.name)
            if task_type in {"watcher", "multi"}:
                if pid is not None:
                    print(f"Stopped watcher for '{args.name}'")
                else:
                    print(f"No watcher found for '{args.name}'")

        print(f"Task '{args.name}' stopped (config preserved)")
        return 0
    except TriggeredTaskError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
