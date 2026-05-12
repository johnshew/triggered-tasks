#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from headless import (
    TriggeredTaskError,
    ensure_logs_dir,
    find_watcher_pid,
    load_task_config,
    logs_root,
    repo_root,
    resolve_task_command,
    stop_watcher,
    tasks_dir,
)

SCRIPT_DIR = Path(__file__).resolve().parent
ACTIVATE_SCRIPT = SCRIPT_DIR / "activate.py"
RUN_SCRIPT = SCRIPT_DIR / "run.py"
STATUS_SCRIPT = SCRIPT_DIR / "status.py"
TEARDOWN_SCRIPT = SCRIPT_DIR / "teardown.py"


class SmokeFailure(RuntimeError):
    pass


def fail(message: str) -> None:
    raise SmokeFailure(message)


def run_status(*args: str) -> str:
    completed = subprocess.run(
        [sys.executable, str(STATUS_SCRIPT), *args],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SmokeFailure(completed.stderr.strip() or completed.stdout.strip() or "status failed")
    return completed.stdout.strip()


def run_task(name: str, changed_file: str | None = None) -> str:
    """Execute a task via run.py and return its stdout."""
    cmd = [sys.executable, str(RUN_SCRIPT), "--name", name]
    if changed_file:
        cmd.extend(["--changed-file", changed_file])
    completed = subprocess.run(
        cmd, cwd=repo_root(), capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SmokeFailure(f"run.py failed for {name}: {detail}")
    return completed.stdout


def read_agent_output_from_log(name: str) -> str:
    """Read the most recent agent output from the task's JSONL log."""
    log_path = logs_root() / f"{name}.log"
    if not log_path.is_file():
        raise SmokeFailure(f"No log file found: {log_path.name}")
    agent_output = ""
    for line in log_path.read_text(encoding="utf-8").strip().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("phase") == "agent" and entry.get("status") == "ok":
            agent_output = entry.get("output", "")
    if not agent_output:
        raise SmokeFailure(f"No successful agent output found in {log_path.name}")
    return agent_output


def cleanup() -> None:
    subprocess.run([sys.executable, str(TEARDOWN_SCRIPT), "--name", "smoketest-cron"], cwd=repo_root(), check=False, capture_output=True, text=True)
    subprocess.run([sys.executable, str(TEARDOWN_SCRIPT), "--name", "smoketest-watcher"], cwd=repo_root(), check=False, capture_output=True, text=True)
    subprocess.run([sys.executable, str(TEARDOWN_SCRIPT), "--name", "smoketest-preprocessor"], cwd=repo_root(), check=False, capture_output=True, text=True)
    subprocess.run([sys.executable, str(TEARDOWN_SCRIPT), "--name", "smoketest-debounce"], cwd=repo_root(), check=False, capture_output=True, text=True)
    subprocess.run([sys.executable, str(TEARDOWN_SCRIPT), "--name", "smoketest-spawn-child"], cwd=repo_root(), check=False, capture_output=True, text=True)
    # Teardown only stops scheduling; remove smoketest files ourselves
    for name in ("smoketest-cron", "smoketest-watcher", "smoketest-preprocessor", "smoketest-debounce", "smoketest-spawn-child"):
        prompt = tasks_dir() / f"{name}.md"
        prompt.unlink(missing_ok=True)
    trigger_dir = logs_root() / "smoketest-watcher"
    trigger_file = trigger_dir / "trigger.txt"
    trigger_file.unlink(missing_ok=True)
    # Clean up debounce test artifacts
    debounce_dir = logs_root() / "smoketest-debounce"
    (debounce_dir / "trigger.txt").unlink(missing_ok=True)
    (logs_root() / "smoketest-debounce-result.txt").unlink(missing_ok=True)
    # Clean up spawn test artifacts
    (logs_root() / "smoketest-spawn-child-result.txt").unlink(missing_ok=True)
    # Clean up pre-processor smoketest artifacts
    for fname in ("smoketest-preprocessor-prep.py", "smoketest-preprocessor-post.sh"):
        handler = repo_root() / "Agents" / "handlers" / fname
        handler.unlink(missing_ok=True)
    # Remove empty smoketest directories
    for d in (trigger_dir, debounce_dir):
        try:
            d.rmdir()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="claude")
    parser.add_argument("--model", default=None, help="Model to use (default: sonnet for claude, claude-haiku-4.5 for copilot)")
    args = parser.parse_args()

    # Pre-flight: fail fast if environment can't support the smoketest.
    # Avoids burning 90s on watcher timeouts in sandboxed environments.
    if not shutil.which("inotifywait"):
        print("FAIL: inotifywait not found. Install: sudo apt install inotify-tools")
        return 1
    # Quick inotify syscall check (sandbox may block even if binary exists)
    try:
        probe = subprocess.run(
            ["inotifywait", "-t", "0", "-e", "close_write", "/dev/null"],
            capture_output=True, timeout=5,
        )
        # exit code 2 = timeout (expected), 0 = event detected, both fine
        if probe.returncode not in (0, 1, 2):
            print(f"FAIL: inotifywait unusable (exit {probe.returncode}). "
                  "This smoketest requires unsandboxed execution.")
            return 1
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"FAIL: inotifywait blocked ({e}). "
              "This smoketest requires unsandboxed execution.")
        return 1

    model = args.model or ("sonnet" if args.agent in ("claude", "agency claude") else "claude-haiku-4.5")

    cron_name = "smoketest-cron"
    watcher_name = "smoketest-watcher"
    trigger_dir = logs_root() / watcher_name
    trigger_file = trigger_dir / "trigger.txt"

    print(f"Using agent: {args.agent}, model: {model}")
    cleanup()

    try:
        print(f"[1/10] Creating test cron task \"{cron_name}\"...")
        ensure_logs_dir()
        (repo_root() / "Agents" / f"{cron_name}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"agent: {args.agent}",
                    f"model: {model}",
                    "mode: plan",
                    "post-processor: write-files.sh",
                    'schedule: "0 0 */3 * *"',
                    "---",
                    "",
                    "# Smoketest Cron Task",
                    "",
                    "This is a local repository smoketest.",
                    "Follow the steps below and return the required JSON exactly.",
                    "",
                    "## Steps",
                    "",
                    "1. Read this prompt completely",
                    "2. Build the JSON object shown below",
                    '3. Keep the magic field exactly "xyzzy"',
                    "4. Keep the file entry exactly as shown",
                    "5. Output the JSON object only",
                    "",
                    "## Required output",
                    "",
                    '{"files":[{"path":"Agents/logs/smoketest-watcher/trigger.txt","content":"smoketest-trigger-fired"}],"summary":"Smoketest trigger file written","magic":"xyzzy"}',
                ]
            ),
            encoding="utf-8",
        )
        print(f"  Created: Agents/{cron_name}.md")

        print("")
        print(f"[2/10] Creating test watcher task \"{watcher_name}\"...")
        trigger_dir.mkdir(parents=True, exist_ok=True)
        (repo_root() / "Agents" / f"{watcher_name}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"agent: {args.agent}",
                    f"model: {model}",
                    "mode: plan",
                    f"watchPath: Agents/logs/{watcher_name}/",
                    "---",
                    "",
                    "# Smoketest Watcher Task",
                    "",
                    "This is a local repository smoketest.",
                    "Follow the steps below and return the required JSON exactly.",
                    "",
                    "## Steps",
                    "",
                    "1. Read the file `Agents/logs/smoketest-watcher/trigger.txt`",
                    '2. Verify its content is "smoketest-trigger-fired"',
                    '3. Build the JSON object shown below, setting status to "pass" or "fail"',
                    '4. Keep the "trigger" and "magic" fields exactly as shown',
                    "5. Output the JSON object only",
                    "",
                    "## Required output",
                    "",
                    '{"status":"pass","trigger":"smoketest-trigger-fired","magic":"xyzzy"}',
                ]
            ),
            encoding="utf-8",
        )
        print(f"  Created: Agents/{watcher_name}.md")

        print("")
        print("[3/10] Verifying status reads frontmatter correctly...")
        cron_status = json.loads(run_status("--json", cron_name))
        print("  Cron task status JSON:")
        print(f"  {json.dumps(cron_status, separators=(',', ':'))}")
        if cron_status.get("type") != "cron":
            fail(f"Expected type=cron, got {cron_status.get('type')}")
        if cron_status.get("agent") != args.agent:
            fail(f"Expected agent={args.agent}")
        if cron_status.get("mode") != "plan":
            fail("Expected mode=plan")
        if cron_status.get("state") != "stopped":
            fail("Expected state=stopped (not yet activated)")
        if cron_status.get("post-processor") != "Agents/handlers/write-files.sh":
            fail("Expected post-processor=Agents/handlers/write-files.sh")
        print("  Cron task: fields verified")

        watcher_status = json.loads(run_status("--json", watcher_name))
        if watcher_status.get("type") != "watcher":
            fail("Expected type=watcher")
        wp = watcher_status.get("watchPath")
        expected_wp = f"Agents/logs/{watcher_name}/"
        if wp != [expected_wp] and wp != expected_wp:
            fail(f"Expected watchPath=[{expected_wp!r}], got {wp!r}")
        print("  Watcher task: fields verified")

        all_status = json.loads(run_status("--json"))
        task_count = sum(
            1 for task in all_status.get("tasks", []) if task.get("name") in {cron_name, watcher_name}
        )
        if task_count != 2:
            fail(f"Expected 2 smoketest tasks in status, got {task_count}")
        print("  All-tasks status: both tasks present")

        resolved = resolve_task_command(cron_name)
        if args.agent in {"claude", "agency claude"} and "--permission-mode plan" not in resolved:
            fail("Missing plan mode flag")
        if args.agent in {"copilot", "agency copilot"} and "--deny-tool shell" not in resolved:
            fail("Missing deny-tool flags")
        print("  Command resolution: OK")
        print("  Status checks: PASS")

        print("")
        print(f"[4/10] Activating watcher via activate.py for \"{watcher_name}\"...")
        if not shutil.which("inotifywait"):
            fail("inotifywait not found. Install with: sudo apt install inotify-tools")
        activate_result = subprocess.run(
            [sys.executable, str(ACTIVATE_SCRIPT), "--name", watcher_name],
            cwd=repo_root(), capture_output=True, text=True, check=False,
        )
        if activate_result.returncode != 0:
            fail(f"activate.py failed: {activate_result.stderr.strip() or activate_result.stdout.strip()}")
        print(f"  {activate_result.stdout.strip()}")
        watcher_pid = find_watcher_pid(watcher_name)
        if watcher_pid is None:
            fail("Watcher process not found after activation")
        print(f"  Watcher confirmed running (pid: {watcher_pid})")

        print("")
        print(f"[5/10] Running cron task via run.py (simulates cron trigger)...")
        print(f"  Invoking: run.py --name {cron_name}")
        run_output = run_task(cron_name)
        for line in run_output.strip().splitlines():
            print(f"  {line}")
        cron_output = read_agent_output_from_log(cron_name)
        print(f"  Agent output from log ({len(cron_output.encode('utf-8'))} bytes)")
        cron_json = json.loads(cron_output)
        if cron_json.get("magic") != "xyzzy":
            fail("Agent output missing magic:xyzzy — JSON may be truncated or wrong")
        print("  Valid JSON with magic:xyzzy verified")
        if not trigger_file.is_file():
            fail("Handler did not write trigger file")
        if trigger_file.read_text(encoding="utf-8").strip() != "smoketest-trigger-fired":
            fail("Trigger content mismatch")
        print("  Trigger file verified")

        print("")
        print("[6/10] Waiting for watcher to detect change and run agent...")
        # The watcher (started via activate.py) should detect the trigger file
        # written by step 5's handler and automatically invoke run.py.
        watcher_log = logs_root() / f"{watcher_name}.log"
        max_wait = 90
        poll_interval = 3
        waited = 0
        watcher_output = ""
        while waited < max_wait:
            if watcher_log.is_file():
                for line in watcher_log.read_text(encoding="utf-8").strip().splitlines():
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("phase") == "agent" and entry.get("status") == "ok":
                        watcher_output = entry.get("output", "")
                    if entry.get("phase") == "done":
                        break
                if watcher_output:
                    break
            time.sleep(poll_interval)
            waited += poll_interval
            if waited % 15 == 0:
                print(f"  Still waiting... ({waited}s)")
        if not watcher_output:
            fail(f"Watcher did not produce agent output within {max_wait}s")
        print(f"  Watcher fired automatically after {waited}s")
        print(f"  Agent output from log: {watcher_output.splitlines()[0]}")
        watcher_json = json.loads(watcher_output)
        if watcher_json.get("magic") != "xyzzy":
            fail("Watcher output missing magic:xyzzy")
        if watcher_json.get("status") != "pass":
            fail("Watcher reported status != pass")
        print("  Watcher confirmed: status=pass, magic=xyzzy")

        print("")
        print("[7/10] Confirming outputs...")
        if not trigger_file.is_file():
            fail("Trigger file missing after run")
        if trigger_file.read_text(encoding="utf-8").strip() != "smoketest-trigger-fired":
            fail("Trigger file content wrong")
        print("  Trigger file: OK")
        cron_log = logs_root() / f"{cron_name}.log"
        if not cron_log.is_file():
            fail("Task log missing — run.py should have created it")
        print(f"  Task log: {cron_log.name} exists")
        watcher_log_file = logs_root() / f"{watcher_name}.log"
        if not watcher_log_file.is_file():
            fail("Watcher log missing — watcher should have created it")
        print(f"  Watcher log: {watcher_log_file.name} exists")
        print("  Logs: OK")
        table_output = run_status()
        if cron_name not in table_output:
            fail("Cron task missing from status table")
        if watcher_name not in table_output:
            fail("Watcher task missing from status table")
        print("  Status table:")
        for line in table_output.splitlines():
            print(f"    {line}")
        print("  Output confirmation: PASS")

        print("")
        print("[8/10] Validating pre-processor → post-processor pipeline (agent: none)...")
        preprocessor_name = "smoketest-preprocessor"
        handlers_dir = repo_root() / "Agents" / "handlers"
        handlers_dir.mkdir(parents=True, exist_ok=True)

        # Create a Python pre-processor that outputs structured JSON
        pre_processor_path = handlers_dir / "smoketest-preprocessor-prep.py"
        pre_processor_path.write_text(
            'import json, sys\n'
            'print(json.dumps({"magic": "pre-xyzzy", "data": [1, 2, 3], "skip": False}))\n',
            encoding="utf-8",
        )

        # Create a shell post-processor that reads pre-processor output from stdin
        # and verifies the magic value
        post_processor_path = handlers_dir / "smoketest-preprocessor-post.sh"
        post_processor_path.write_text(
            '#!/bin/bash\n'
            'INPUT=$(cat)\n'
            'if echo "$INPUT" | grep -q "pre-xyzzy"; then\n'
            '  echo "post-processor: received pre-processor data with magic"\n'
            'else\n'
            '  echo "post-processor: MISSING pre-processor data" >&2\n'
            '  exit 1\n'
            'fi\n',
            encoding="utf-8",
        )
        post_processor_path.chmod(0o755)

        (repo_root() / "Agents" / f"{preprocessor_name}.md").write_text(
            "\n".join([
                "---",
                "agent: none",
                "pre-processor: smoketest-preprocessor-prep.py",
                "post-processor: smoketest-preprocessor-post.sh",
                'schedule: "0 0 1 1 *"',
                "---",
                "",
                "# Smoketest Pre-processor Task",
                "",
                "Validates the pre-processor → post-processor pipeline with agent: none.",
            ]),
            encoding="utf-8",
        )
        print(f"  Created: pre-processor ({pre_processor_path.name}), post-processor ({post_processor_path.name}), task ({preprocessor_name}.md)")

        # Clear any stale log from previous runs
        preprocessor_log = logs_root() / f"{preprocessor_name}.log"
        preprocessor_log.unlink(missing_ok=True)

        preprocessor_output = run_task(preprocessor_name)
        print(f"  run.py output:")
        for line in preprocessor_output.strip().splitlines():
            print(f"    {line}")

        # Verify pre-processor phase was logged
        preprocessor_log = logs_root() / f"{preprocessor_name}.log"
        if not preprocessor_log.is_file():
            fail("Pre-processor task log not created")
        pre_phase_found = False
        post_phase_found = False
        for line in preprocessor_log.read_text(encoding="utf-8").strip().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("phase") == "pre-processor" and entry.get("status") == "ok":
                pre_phase_found = True
                pre_output = entry.get("output", "")
                if "pre-xyzzy" not in pre_output:
                    fail("Pre-processor log output missing magic value")
            if entry.get("phase") == "post-processor" and entry.get("status") == "ok":
                post_phase_found = True
        if not pre_phase_found:
            fail("No pre-processor phase logged in JSONL")
        if not post_phase_found:
            fail("No post-processor phase logged in JSONL")
        print("  Pre-processor logged: OK")
        print("  Post-processor received pre-processor data: OK")
        print("  pre-processor → post-processor (agent: none): PASS")

        print("")
        print("[9/10] Validating pre-processor skip gating...")
        # Rewrite the pre-processor to output skip: true
        pre_processor_path.write_text(
            'import json\n'
            'print(json.dumps({"skip": True, "reason": "smoketest-skip"}))\n',
            encoding="utf-8",
        )
        # Clear the log for a fresh run
        preprocessor_log.unlink(missing_ok=True)
        skip_output = run_task(preprocessor_name)
        print(f"  run.py output:")
        for line in skip_output.strip().splitlines():
            print(f"    {line}")

        # Verify skip was logged and post-processor was NOT called
        skip_logged = False
        post_after_skip = False
        for line in preprocessor_log.read_text(encoding="utf-8").strip().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("phase") == "pre-processor" and entry.get("skip") is True:
                skip_logged = True
            if entry.get("phase") == "post-processor":
                post_after_skip = True
        if not skip_logged:
            fail("Pre-processor skip not logged")
        if post_after_skip:
            fail("Post-processor ran despite pre-processor skip=true")
        print("  Skip gating: PASS")

        print("")
        print("[10/12] Validating spawn module (cgroup-survival path)...")
        # Tests the shared spawn utility from its portable location:
        # - find_uv() resolves the binary in any context
        # - spawn_agent_task() launches a detached child that completes
        # When running inside a systemd unit (INVOCATION_ID set), the child
        # is wrapped in systemd-run --user --scope for cgroup survival.
        sys.path.insert(0, str(SCRIPT_DIR))
        from spawn import find_uv, spawn_agent_task

        uv_path = find_uv()
        print(f"  find_uv() -> {uv_path}")
        if not Path(uv_path).is_file():
            fail(f"find_uv() returned non-existent path: {uv_path}")

        # Create a minimal task for spawn to invoke
        spawn_task_name = "smoketest-spawn-child"
        spawn_result_file = logs_root() / f"{spawn_task_name}-result.txt"
        spawn_result_file.unlink(missing_ok=True)
        (tasks_dir() / f"{spawn_task_name}.md").write_text(
            "\n".join([
                "---",
                f"agent: {args.agent}",
                f"model: {model}",
                "mode: plan",
                "post-processor: write-files.sh",
                'schedule: "0 0 1 1 *"',
                "---",
                "",
                "# Smoketest Spawn Child",
                "",
                "Output exactly this JSON:",
                "",
                f'{{"files":[{{"path":"Agents/logs/{spawn_task_name}-result.txt","content":"spawn-passed"}}],"summary":"Spawn child completed","magic":"spawn-xyzzy"}}',
            ]),
            encoding="utf-8",
        )

        in_systemd = bool(os.environ.get("INVOCATION_ID"))
        print(f"  INVOCATION_ID present: {in_systemd} ({'scope wrapping active' if in_systemd else 'direct spawn'})")

        proc = spawn_agent_task(
            repo_root(), spawn_task_name, ["smoketest-spawn-trigger.md"],
            quiet=True,
        )
        if proc is None:
            fail("spawn_agent_task() returned None - check spawn-stderr.log")
        print(f"  Spawned child PID={proc.pid}")

        # Wait for the child to produce output
        spawn_log = logs_root() / f"{spawn_task_name}.log"
        max_wait = 90
        poll_interval = 3
        waited = 0
        while waited < max_wait:
            if spawn_result_file.is_file():
                break
            time.sleep(poll_interval)
            waited += poll_interval
            if waited % 15 == 0:
                print(f"  Still waiting... ({waited}s)")
        if not spawn_result_file.is_file():
            log_tail = ""
            if spawn_log.is_file():
                log_tail = spawn_log.read_text(encoding="utf-8").strip()[-500:]
            fail(f"Spawn child did not produce result within {max_wait}s. Log tail: {log_tail}")
        content = spawn_result_file.read_text(encoding="utf-8").strip()
        if content != "spawn-passed":
            fail(f"Spawn result content wrong: {content!r}")
        print(f"  Child completed ({waited}s), result verified")

        # Cleanup spawn test artifacts
        spawn_result_file.unlink(missing_ok=True)
        subprocess.run([sys.executable, str(TEARDOWN_SCRIPT), "--name", spawn_task_name],
                       cwd=repo_root(), check=False, capture_output=True, text=True)
        (tasks_dir() / f"{spawn_task_name}.md").unlink(missing_ok=True)
        print("  Spawn module (cgroup-survival): PASS")

        print("")
        print("[11/12] Validating debounced watcher dispatch via systemd timer...")
        # This tests the full debounce path: watcher fires, but instead of
        # immediate dispatch, a systemd timer is scheduled. Trigger twice
        # rapidly, verify only one agent run happens after the quiet window.
        # This exercises the same code path that failed in production (cgroup
        # teardown killing spawned agents).
        debounce_name = "smoketest-debounce"
        debounce_dir = logs_root() / debounce_name
        debounce_dir.mkdir(parents=True, exist_ok=True)
        debounce_trigger = debounce_dir / "trigger.txt"
        debounce_trigger.unlink(missing_ok=True)

        # Post-processor: write-files.sh (already exists) writes the output.
        # Result file is written OUTSIDE the watched directory to avoid
        # self-triggering the watcher.
        (tasks_dir() / f"{debounce_name}.md").write_text(
            "\n".join([
                "---",
                f"agent: {args.agent}",
                f"model: {model}",
                "mode: plan",
                "post-processor: write-files.sh",
                f"watchPath: Agents/logs/{debounce_name}/",
                "debounce: 5",
                "---",
                "",
                "# Smoketest Debounce Task",
                "",
                "This is a smoketest for debounced watcher dispatch.",
                "Read the trigger file, verify it exists, and output JSON.",
                "",
                "## Steps",
                "",
                f'1. Read the file `Agents/logs/{debounce_name}/trigger.txt`',
                "2. Build the JSON object shown below",
                '3. Keep the magic field exactly "debounce-xyzzy"',
                "4. Output the JSON object only",
                "",
                "## Required output",
                "",
                f'{{"files":[{{"path":"Agents/logs/{debounce_name}-result.txt","content":"debounce-passed"}}],"summary":"Debounce test passed","magic":"debounce-xyzzy"}}',
            ]),
            encoding="utf-8",
        )
        print(f"  Created: {debounce_name}.md (debounce: 5s)")

        # Activate the watcher
        activate_result = subprocess.run(
            [sys.executable, str(ACTIVATE_SCRIPT), "--name", debounce_name],
            cwd=repo_root(), capture_output=True, text=True, check=False,
        )
        if activate_result.returncode != 0:
            fail(f"activate.py failed for debounce task: {activate_result.stderr.strip()[:200]}")
        debounce_pid = find_watcher_pid(debounce_name)
        if debounce_pid is None:
            fail("Debounce watcher process not found after activation")
        print(f"  Watcher activated (pid: {debounce_pid})")

        # Trigger three times to exercise BOTH debounce layers:
        #   Layer 1: 1s internal batch debounce (DEBOUNCE_SECS in activate.py)
        #   Layer 2: 5s systemd timer debounce (frontmatter debounce: 5)
        # T=0.0 and T=0.5 land in the same 1s batch (tests layer 1).
        # T=5.0 creates a NEW batch that reschedules the timer (tests layer 2).
        debounce_trigger.write_text("trigger-1", encoding="utf-8")
        time.sleep(0.5)
        debounce_trigger.write_text("trigger-2", encoding="utf-8")
        time.sleep(4.5)
        debounce_trigger.write_text("trigger-3", encoding="utf-8")
        print("  Triggered three times (0s, 0.5s, 5s) - exercises both debounce layers")

        # Wait for the debounce timer to fire and the agent to complete.
        # Timer reschedules at T=5, fires at T=5 + debounce(5) + buffer(2) = T~12.
        # Plus agent runtime = ~30-60s total from last trigger.
        debounce_result_file = logs_root() / f"{debounce_name}-result.txt"
        debounce_log = logs_root() / f"{debounce_name}.log"
        max_wait = 120
        poll_interval = 3
        waited = 0
        while waited < max_wait:
            if debounce_result_file.is_file():
                break
            time.sleep(poll_interval)
            waited += poll_interval
            if waited % 15 == 0:
                print(f"  Still waiting... ({waited}s)")
        if not debounce_result_file.is_file():
            # Check log for clues
            log_tail = ""
            if debounce_log.is_file():
                log_tail = debounce_log.read_text(encoding="utf-8").strip()[-500:]
            fail(f"Debounce task did not produce result within {max_wait}s. Log tail: {log_tail}")
        content = debounce_result_file.read_text(encoding="utf-8").strip()
        if content != "debounce-passed":
            fail(f"Debounce result content wrong: {content!r}")
        print(f"  Agent completed via debounce timer ({waited}s)")

        # Verify only one successful agent run (not two)
        agent_runs = 0
        if debounce_log.is_file():
            for line in debounce_log.read_text(encoding="utf-8").strip().splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("phase") == "agent" and entry.get("status") == "ok":
                    agent_runs += 1
        if agent_runs > 1:
            fail(f"Expected 1 agent run (debounced), got {agent_runs}")
        print(f"  Confirmed: {agent_runs} agent run (three triggers debounced into one)")
        print("  Debounced watcher dispatch: PASS")

        # Cleanup debounce test
        stop_watcher(debounce_name)
        subprocess.run([sys.executable, str(TEARDOWN_SCRIPT), "--name", debounce_name],
                       cwd=repo_root(), check=False, capture_output=True, text=True)
        (tasks_dir() / f"{debounce_name}.md").unlink(missing_ok=True)
        debounce_trigger.unlink(missing_ok=True)
        debounce_result_file.unlink(missing_ok=True)

        print("")
        print("[12/12] Tearing down test tasks...")
        stop_watcher(watcher_name)
        subprocess.run([sys.executable, str(TEARDOWN_SCRIPT), "--name", cron_name], cwd=repo_root(), check=False, capture_output=True, text=True)
        subprocess.run([sys.executable, str(TEARDOWN_SCRIPT), "--name", watcher_name], cwd=repo_root(), check=False, capture_output=True, text=True)
        subprocess.run([sys.executable, str(TEARDOWN_SCRIPT), "--name", preprocessor_name], cwd=repo_root(), check=False, capture_output=True, text=True)
        # Teardown only stops scheduling; remove smoketest files ourselves
        for name in (cron_name, watcher_name, preprocessor_name):
            prompt = tasks_dir() / f"{name}.md"
            prompt.unlink(missing_ok=True)
        # Remove pre-processor smoketest processor scripts
        pre_processor_path.unlink(missing_ok=True)
        post_processor_path.unlink(missing_ok=True)
        if (tasks_dir() / f"{cron_name}.md").exists():
            fail("Cron task file still exists after cleanup")
        if (tasks_dir() / f"{watcher_name}.md").exists():
            fail("Watcher task file still exists after cleanup")
        if (tasks_dir() / f"{preprocessor_name}.md").exists():
            fail("Pre-processor task file still exists after cleanup")
        final_status = json.loads(run_status("--json"))
        remaining = len([task for task in final_status.get("tasks", []) if task.get("name") in {cron_name, watcher_name, preprocessor_name}])
        if remaining != 0:
            fail("Tasks still appear in status after teardown")
        print("  Cleanup verified: all tasks removed from disk and status")
        print("  Log files preserved in Agents/logs/")

        print("")
        print(f"PASS - full chain validated ({args.agent}):")
        print(f"  create -> frontmatter -> status -> activate watcher -> {args.agent} CLI -> JSON output ->")
        print(f"  post-processor -> file write -> watcher detect -> auto-run -> confirm outputs ->")
        print(f"  pre-processor -> post-processor (agent:none) -> skip gating ->")
        print(f"  spawn module (cgroup survival) -> debounced dispatch (systemd timer) -> teardown")
        return 0
    except (SmokeFailure, TriggeredTaskError, json.JSONDecodeError) as exc:
        print(f"  FAIL: {exc}", file=sys.stderr)
        try:
            stop_watcher(watcher_name)
        except Exception:
            pass
        try:
            stop_watcher("smoketest-debounce")
        except Exception:
            pass
        cleanup()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
