#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
from __future__ import annotations

import argparse
import hashlib
import os
import json
import shlex
import shutil
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

from headless import (
    MAX_LOG_FIELD_LENGTH,
    TaskConfig,
    TriggeredTaskError,
    add_desired_cron,
    add_desired_watcher,
    clean_path,
    current_crontab_lines,
    ensure_logs_dir,
    find_watcher_pid,
    install_crontab,
    list_tasks,
    load_task_config,
    log_event,
    logs_root,
    repo_root,
    stop_watcher,
    system_log,
)

SCRIPT_PATH = Path(__file__).resolve()
RUN_ONCE_PATH = SCRIPT_PATH.with_name("run.py")


# --- Content-hash cascade guard helpers ---

def _hash_file_content(filepath: Path) -> str | None:
    """SHA-256 hex digest of file content, or None if unreadable."""
    try:
        return hashlib.sha256(filepath.read_bytes()).hexdigest()
    except (OSError, FileNotFoundError):
        return None


def _watch_hash_path(name: str) -> Path:
    return repo_root() / "Agents" / "data" / f"{name}-watch-hashes.json"


def _load_watch_hashes(name: str) -> dict:
    p = _watch_hash_path(name)
    try:
        return json.loads(p.read_text()) if p.is_file() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_watch_hashes(name: str, hashes: dict) -> None:
    # Prune entries for files that no longer exist (stale after renames)
    files = hashes.get("files", {})
    root = repo_root()
    pruned = {f: h for f, h in files.items() if (root / f).exists()}
    if len(pruned) < len(files):
        hashes = {**hashes, "files": pruned}
    p = _watch_hash_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(hashes, indent=2) + "\n")


# Only apply hash filtering within this window after the last dispatch.
# Cascades happen within seconds; intentional touches come later.
CASCADE_WINDOW_SECS = 120


def _validate_handler_paths(config: TaskConfig) -> None:
    """Verify that all referenced handler files exist before activation.

    Raises :class:`TriggeredTaskError` if a pre-processor or post-processor
    file is configured but missing on disk.
    """
    if config.pre_processor_path and not config.pre_processor_path.is_file():
        raise TriggeredTaskError(
            f"pre-processor not found: {config.pre_processor_reference} "
            f"(task '{config.name}')"
        )
    if config.handler_path and not config.handler_path.is_file():
        raise TriggeredTaskError(
            f"handler/post-processor not found: {config.handler_reference} "
            f"(task '{config.name}')"
        )


def install_cron_task(name: str) -> str:
    config = load_task_config(name)
    if not config.schedule:
        raise TriggeredTaskError(f"task '{name}' has no schedule")
    _validate_handler_paths(config)

    ensure_logs_dir()
    repo = repo_root()
    uv = shutil.which("uv") or "uv"
    run_command = [
        uv,
        "run",
        "--script",
        str(RUN_ONCE_PATH),
        "--name",
        name,
        "--quiet",
    ]
    path_line = f"PATH={clean_path()}"
    cron_line = (
        f"{config.schedule} cd {shlex.quote(str(repo))} && "
        f"{shlex.join(run_command)} 2>&1"
    )

    lines = [line for line in current_crontab_lines()
             if name not in line and not line.startswith("PATH=")]
    lines.insert(0, path_line)
    lines.append(cron_line)
    install_crontab(lines)
    return cron_line


# --- Debounce dispatch via systemd timer ---

_debounce_pending: dict[str, list[str]] = {}  # task name -> accumulated changed files


def _debounce_timer_unit(name: str) -> str:
    """Deterministic systemd transient unit name for a task's debounce timer."""
    import re
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", name)
    return f"debounce-{slug}"


def _user_systemd_env() -> dict[str, str]:
    """Return an env dict that can reach the user-level systemd via D-Bus.

    When the watcher is launched from cron (the hourly health-check
    re-activates dead watchers), the cron-spawned process inherits cron's
    minimal env: no DBUS_SESSION_BUS_ADDRESS and no XDG_RUNTIME_DIR. With
    those missing, every `systemd-run --user` / `systemctl --user` call
    fails with "Failed to connect to bus: No medium found", silently
    breaking the watcher's debounce dispatch.

    Synthesize the standard locations from the current uid so the calls
    work regardless of how the watcher was started. Real user-session
    values, when present, are preserved.
    """
    env = os.environ.copy()
    uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                   f"unix:path=/run/user/{uid}/bus")
    return env


def _debounce_schedule(name: str, debounce_secs: int, changed_files: list[str], uv: str) -> None:
    """Schedule (or reschedule) a systemd timer to dispatch the task.

    Accumulates changed_files across multiple calls. Each call resets the
    timer so the task only dispatches after the quiet window expires.
    On fire, the timer invokes run.py with all accumulated changed files.
    """
    # Accumulate changed files
    if name not in _debounce_pending:
        _debounce_pending[name] = []
    for f in changed_files:
        if f not in _debounce_pending[name]:
            _debounce_pending[name].append(f)

    unit = _debounce_timer_unit(name)
    all_files_json = json.dumps(_debounce_pending[name])

    # Synthesize the user-bus env so cron-spawned watchers can reach
    # user-level systemd. Without this, every `systemctl --user` /
    # `systemd-run --user` call below fails with
    # "Failed to connect to bus: No medium found".
    sd_env = _user_systemd_env()

    # Cancel any existing timer and service (idempotent reschedule).
    # Stop the timer first, then stop a possibly-running service from a
    # previous timer fire, and finally clear any failed state on both units.
    # This prevents "Unit already loaded" errors when systemd-run tries to
    # create a new transient unit with the same name.
    subprocess.run(
        ["systemctl", "--user", "stop", f"{unit}.timer"],
        capture_output=True, check=False, env=sd_env,
    )
    subprocess.run(
        ["systemctl", "--user", "stop", f"{unit}.service"],
        capture_output=True, check=False, env=sd_env,
    )
    subprocess.run(
        ["systemctl", "--user", "reset-failed", f"{unit}.timer"],
        capture_output=True, check=False, env=sd_env,
    )
    subprocess.run(
        ["systemctl", "--user", "reset-failed", f"{unit}.service"],
        capture_output=True, check=False, env=sd_env,
    )

    delay = debounce_secs + 2  # small buffer past the debounce window

    result = subprocess.run(
        [
            "systemd-run", "--user",
            f"--unit={unit}",
            f"--on-active={delay}s",
            "--working-directory", str(repo_root()),
            "--setenv", f"PATH={clean_path()}",
            "--setenv", f"HOME={os.environ.get('HOME', str(Path.home()))}",
            "--setenv", f"PYTHONPYCACHEPREFIX={os.environ.get('PYTHONPYCACHEPREFIX', '/tmp/pycache')}",
            "--",
            uv, "run", "--script", str(RUN_ONCE_PATH),
            "--name", name,
            "--changed-files", all_files_json,
            "--quiet",
        ],
        capture_output=True, check=False, env=sd_env,
    )
    if result.returncode == 0:
        log_event(
            logs_root() / f"{name}.log",
            level="info", phase="watcher",
            message=f"debounce timer scheduled: {unit} in {delay}s "
                    f"({len(_debounce_pending[name])} file(s) accumulated)",
        )
        # Clear accumulated files only on success (timer captures the snapshot)
        _debounce_pending[name] = []
    else:
        stderr_msg = result.stderr.decode(errors="replace").strip()[:200]
        log_event(
            logs_root() / f"{name}.log",
            level="error", phase="watcher",
            message=f"debounce timer failed: {stderr_msg}",
            error_category="systemd_unavailable",
        )
        # Keep accumulated files for retry on next trigger


def _validate_watcher_prereqs(config: TaskConfig) -> list[Path]:
    """Return list of absolute paths to watch (dirs and/or files).

    For files, we watch the parent directory and filter by filename in
    the event loop (inotifywait on a single file breaks on atomic saves
    that replace the inode).
    """
    all_paths = config.all_watch_paths
    if not all_paths:
        raise TriggeredTaskError(f"task '{config.name}' has no watchPath")

    watch_targets: list[Path] = []

    for wp in all_paths:
        abs_path = config.watch_path_absolute_for(wp)
        if abs_path.is_file() or abs_path.suffix:
            # Watch parent dir; we'll filter by filename in the event loop
            abs_path.parent.mkdir(parents=True, exist_ok=True)
        elif not abs_path.is_dir():
            abs_path.mkdir(parents=True, exist_ok=True)
            print(f"Created missing watch directory: {wp}")
        watch_targets.append(abs_path)

    if not shutil.which("inotifywait"):
        raise TriggeredTaskError("inotifywait not found")
    return watch_targets


def should_ignore_watch_change(changed_file: str, watch_ignore: list[str] | None = None) -> bool:
    changed_path = Path(changed_file)
    if not changed_path.is_absolute():
        changed_path = (repo_root() / changed_path).resolve()

    try:
        relative = changed_path.relative_to(repo_root())
    except ValueError:
        return False

    if any(part.startswith(".") for part in relative.parts):
        return True
    if any(part == "__pycache__" for part in relative.parts):
        return True
    # Ignore JSONL log files written by the triggered-task system itself
    # (prevents recursive triggers), but allow other files under Agents/logs/
    # so watcher tasks that deliberately watch subdirectories still fire.
    if relative.suffix == ".log" and (
        relative == Path("Agents/logs") / relative.name
        or Path("Agents/logs") in relative.parents
    ):
        return True
    if watch_ignore and relative.name in watch_ignore:
        return True
    # Support directory-prefix ignores: entries ending with '/' match any
    # file whose repo-relative path starts with that prefix.
    if watch_ignore:
        rel_str = relative.as_posix()
        for pattern in watch_ignore:
            if pattern.endswith("/") and (rel_str + "/").startswith(pattern):
                return True
    return False


def watch_loop(name: str) -> int:
    config = load_task_config(name)
    watch_targets = _validate_watcher_prereqs(config)

    ensure_logs_dir()

    # Redirect our own stderr to the task log so crashes are captured as
    # structured JSONL instead of going to a separate .out file.
    import atexit
    import io
    import traceback

    _orig_stderr = sys.stderr
    _stderr_buf = io.StringIO()
    sys.stderr = _stderr_buf

    def _flush_stderr_to_log() -> None:
        text = _stderr_buf.getvalue().strip()
        if text:
            log_event(config.task_log, level="error", phase="watcher",
                      message=text[:20_000])
    atexit.register(_flush_stderr_to_log)

    # Separate file targets (watch parent dir + filter) from directory targets.
    watch_dirs: list[Path] = []
    target_filenames: set[str] = set()
    for t in watch_targets:
        if t.is_file() or t.suffix:
            target_filenames.add(t.name)
            watch_dirs.append(t.parent)
        else:
            watch_dirs.append(t)

    # Deduplicate dirs
    seen: set[Path] = set()
    unique_dirs: list[Path] = []
    for d in watch_dirs:
        if d not in seen:
            seen.add(d)
            unique_dirs.append(d)

    # Events: close_write (direct writes), moved_to (atomic saves and
    # files arriving via temp+rename), moved_from (files leaving a
    # watched directory), delete (files removed from a watched directory).
    # moved_to is always included - atomic writes (os.replace) into a
    # watched directory only produce moved_to, not close_write.
    events = "close_write,moved_to,moved_from,delete"
    inotify_args = [
        "inotifywait", "-m", "-r", "-e", events,
        *[str(d) for d in unique_dirs], "--format", "%w%f",
    ]

    process = subprocess.Popen(
        inotify_args,
        cwd=repo_root(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    watch_desc = ", ".join(str(d) for d in unique_dirs)
    if target_filenames:
        watch_desc += f" (filtering: {', '.join(sorted(target_filenames))})"
    log_event(config.task_log, level="info", phase="watcher",
             message=f"inotifywait started, watching {watch_desc}")

    shutdown_requested = False

    def handle_shutdown(signum: int, _frame: types.FrameType | None) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        if process.stdout is None:
            raise TriggeredTaskError("watcher stdout was not available")

        import select
        import fcntl

        # Use the raw file descriptor for non-blocking I/O.
        # TextIOWrapper.read() can buffer internally and miss select()
        # readiness, so we use os.read() on the raw fd instead.
        fd = process.stdout.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        DEBOUNCE_SECS = 1.0  # collect events for this long before firing

        pending_line = ""
        while True:
            # Block until at least one event arrives
            select.select([fd], [], [])
            if shutdown_requested:
                return 0

            # Read all immediately available events
            changed_files: list[str] = []
            try:
                while True:
                    raw = os.read(fd, 8192)
                    if not raw:
                        # EOF — inotifywait exited
                        inotify_stderr = ""
                        if process.stderr:
                            try:
                                inotify_stderr = process.stderr.read().strip()
                            except Exception:
                                pass
                        log_event(config.task_log, level="warning", phase="watcher",
                                  message=f"inotifywait exited (rc={process.poll()})"
                                  + (f": {inotify_stderr[:MAX_LOG_FIELD_LENGTH]}" if inotify_stderr else ""))
                        return 0
                    chunk = raw.decode("utf-8", errors="replace")
                    pending_line += chunk
                    while "\n" in pending_line:
                        line, pending_line = pending_line.split("\n", 1)
                        f = line.strip()
                        if not f:
                            continue
                        # File-target filter: when watching a parent dir on behalf of
                        # a specific file, only accept events for those files. Events
                        # from recursively-watched subdirs of directory targets pass through.
                        if target_filenames:
                            changed = Path(f)
                            # Accept if the file matches a target filename, or if
                            # it's under a directory target (not a file-target parent)
                            if changed.name not in target_filenames:
                                # Check if this event is from a dir we're watching recursively
                                is_under_dir_target = any(
                                    t.is_dir() and str(changed).startswith(str(t))
                                    for t in watch_targets
                                )
                                if not is_under_dir_target:
                                    continue
                        if not should_ignore_watch_change(f, config.watch_ignore):
                            changed_files.append(f)
            except (BlockingIOError, IOError):
                pass

            if not changed_files:
                continue

            # Wait briefly for more events to arrive (debounce)
            import time
            deadline = time.monotonic() + DEBOUNCE_SECS
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([fd], [], [], remaining)
                if shutdown_requested:
                    return 0
                if not ready:
                    break
                try:
                    raw = os.read(fd, 8192)
                    if not raw:
                        break
                    chunk = raw.decode("utf-8", errors="replace")
                    pending_line += chunk
                    while "\n" in pending_line:
                        line, pending_line = pending_line.split("\n", 1)
                        f = line.strip()
                        if not f:
                            continue
                        if target_filenames:
                            changed = Path(f)
                            if changed.name not in target_filenames:
                                is_under_dir_target = any(
                                    t.is_dir() and str(changed).startswith(str(t))
                                    for t in watch_targets
                                )
                                if not is_under_dir_target:
                                    continue
                        if not should_ignore_watch_change(f, config.watch_ignore):
                            changed_files.append(f)
                except (BlockingIOError, IOError):
                    break

            # Deduplicate, keep all unique changed files — convert to repo-relative paths
            root = repo_root()
            unique_files = list(dict.fromkeys(changed_files))
            relative_files = []
            for f in unique_files:
                try:
                    relative_files.append(str(Path(f).relative_to(root)))
                except ValueError:
                    relative_files.append(f)
            # Content-hash cascade guard: drop individual files whose
            # content hasn't changed since the last dispatch for this task.
            # Only active within CASCADE_WINDOW_SECS of the last dispatch —
            # outside the window, every event dispatches (so touch works).
            cached = _load_watch_hashes(name)
            cached_hashes: dict[str, str] = cached.get("files", {})
            last_dispatch = cached.get("_dispatched_at", 0)
            in_cascade_window = (time.time() - last_dispatch) < CASCADE_WINDOW_SECS

            current_hashes: dict[str, str] = {}
            actually_changed: list[str] = []
            hash_skipped: list[str] = []
            for f in relative_files:
                h = _hash_file_content(root / f)
                if h:
                    current_hashes[f] = h
                    if in_cascade_window and cached_hashes.get(f) == h:
                        hash_skipped.append(f)
                    else:
                        actually_changed.append(f)
                else:
                    # Can't hash (deleted/unreadable) — keep it in the batch
                    actually_changed.append(f)

            if hash_skipped:
                short = {f: current_hashes[f][:12] for f in hash_skipped}
                log_event(config.task_log, level="info", phase="watcher",
                          message=f"content-hash filtered: {len(hash_skipped)} file(s) unchanged",
                          skipped_files=hash_skipped, hashes=short)

            if not actually_changed:
                log_event(system_log(), level="info", task=name, phase="watcher",
                          message=f"content-hash skip: all {len(relative_files)} file(s) unchanged since last dispatch",
                          changed_files=relative_files)
                continue

            # Cache current hashes + dispatch timestamp
            _save_watch_hashes(name, {
                "files": {**cached_hashes, **current_hashes},
                "_dispatched_at": time.time(),
            })

            all_files_json = json.dumps(actually_changed)
            short_hashes = {f: current_hashes[f][:12] for f in actually_changed if f in current_hashes}
            log_event(system_log(), level="info", task=name, phase="watcher",
                      message=f"debounce batch: {len(actually_changed)} file(s)" +
                              (f" ({len(hash_skipped)} hash-filtered)" if hash_skipped else ""),
                      changed_files=actually_changed, hashes=short_hashes)
            uv = shutil.which("uv") or "uv"

            if config.debounce:
                # Debounced dispatch: schedule (or reschedule) a systemd timer.
                # Each new batch resets the timer. When it fires, it runs the
                # task with all accumulated changed files.
                _debounce_schedule(name, config.debounce, actually_changed, uv)
            else:
                # Immediate dispatch (default)
                subprocess.run(
                    [
                        uv,
                        "run",
                        "--script",
                        str(RUN_ONCE_PATH),
                        "--name",
                        name,
                        "--changed-files",
                        all_files_json,
                        "--quiet",
                    ],
                    cwd=repo_root(),
                    check=False,
                )
    except SystemExit:
        raise
    except Exception:
        log_event(config.task_log, level="error", phase="watcher",
                  message=traceback.format_exc()[:20_000])
        raise
    finally:
        sys.stderr = _orig_stderr
        process.terminate()
    return 0


def activate_watcher(name: str) -> int:
    config = load_task_config(name)
    _validate_handler_paths(config)
    _validate_watcher_prereqs(config)

    existing_pid = find_watcher_pid(name)
    if existing_pid is not None:
        print("Warning: stopping existing watcher", file=sys.stderr)
        stop_watcher(name)

    ensure_logs_dir()
    uv = shutil.which("uv") or "uv"
    with open("/dev/null", "w") as devnull:
        process = subprocess.Popen(
            [uv, "run", "--script", str(SCRIPT_PATH), "--watch-loop", name],
            cwd=repo_root(),
            stdout=devnull,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    time.sleep(1)
    if process.poll() is not None:
        # Process died — capture stderr and log it
        stderr_text = ""
        if process.stderr:
            stderr_text = process.stderr.read().strip()
            process.stderr.close()
        task_log = logs_root() / f"{name}.log"
        if stderr_text:
            log_event(task_log, level="error", phase="watcher",
                      status="crash", message=stderr_text[:2000])
        raise TriggeredTaskError(
            f"watcher process exited immediately with status {process.returncode}"
            + (f": {stderr_text[:200]}" if stderr_text else ""))

    # Process is alive — close our end of the pipe.
    # watch_loop has already redirected sys.stderr to StringIO,
    # so nothing writes to fd2 after startup.
    if process.stderr:
        process.stderr.close()

    return process.pid


def activate_one(name: str) -> list[str]:
    """Activate a single task. Returns list of activated trigger types."""
    config = load_task_config(name)
    ensure_logs_dir()
    activated = []
    if config.schedule:
        cron_line = install_cron_task(config.name)
        add_desired_cron(config.name)
        log_event(system_log(), level="info", task=config.name, phase="activate", type="cron",
                  schedule=config.schedule)
        print(f"Activated cron for '{config.name}': {cron_line}")
        activated.append("cron")

    if config.watch_path:
        pid = activate_watcher(config.name)
        add_desired_watcher(config.name)
        log_event(system_log(), level="info", task=config.name, phase="activate", type="watcher",
                  watchPath=config.watch_path, pid=pid)
        print(f"Activated watcher for '{config.name}': watching {config.watch_path}, pid {pid}")
        activated.append("watcher")

    return activated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name")
    parser.add_argument("--all", action="store_true", help="Activate all tasks that have a schedule or watchPath")
    parser.add_argument("--watch-loop", dest="watch_loop_name")
    args = parser.parse_args()

    try:
        if args.watch_loop_name:
            task_log = logs_root() / f"{args.watch_loop_name}.log"
            try:
                return watch_loop(args.watch_loop_name)
            except Exception:
                import traceback
                ensure_logs_dir()
                log_event(task_log, level="error", phase="watcher",
                          status="crash", message=traceback.format_exc()[:2000])
                return 1

        if args.all:
            errors = []
            total = 0
            for name in list_tasks():
                try:
                    config = load_task_config(name)
                except TriggeredTaskError:
                    continue
                if not config.schedule and not config.watch_path:
                    continue
                try:
                    activated = activate_one(name)
                    if activated:
                        total += 1
                except TriggeredTaskError as exc:
                    print(f"  FAILED {name}: {exc}", file=sys.stderr)
                    errors.append(name)
            print(f"\nActivated {total} tasks" + (f" ({len(errors)} failed)" if errors else ""))
            return 1 if errors else 0

        if not args.name:
            raise TriggeredTaskError("--name or --all is required")

        activated = activate_one(args.name)
        if not activated:
            raise TriggeredTaskError(
                f"task '{args.name}' has no schedule or watchPath"
            )
        return 0
    except TriggeredTaskError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
