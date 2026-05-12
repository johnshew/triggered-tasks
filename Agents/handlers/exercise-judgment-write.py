#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""
exercise-judgment-write.py - Post-processor for exercise-judgment.

Receives the agent's JSON output on stdin:
  {"action": "update", "recommendations": "...", "summary": "..."}
  {"action": "no-change", "summary": "..."}

Merges with current R checkbox state, writes R atomically,
records last_judged_state_hash, and clears judgment_spawned_for_hash.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXERCISE_DIR = REPO_ROOT / "Exercise"
DATA_DIR = EXERCISE_DIR / "data"

RECS_MD = EXERCISE_DIR / "Recommendations.md"
STATE_MD = EXERCISE_DIR / "State.md"

JUDGED_STATE_HASH_FILE = DATA_DIR / "last_judged_state_hash"
SPAWNED_FOR_FILE = DATA_DIR / "judgment_spawned_for_hash"
JUDGMENT_RECS_HASH_FILE = DATA_DIR / "judgment_recs_hash"

sys.path.insert(0, str(REPO_ROOT / "Agents" / "lib"))
sys.path.insert(0, str(REPO_ROOT / "Agents" / "handlers"))
sys.path.insert(0, str(REPO_ROOT / ".claude" / "skills" / "triggered-task" / "scripts"))
from sync_guards import hash_file, atomic_write, clear_spawn, record_spawn
from checkbox_helpers import CB_RE, checkbox_key, extract_checkboxes
from spawn import spawn_agent_task


def log(msg: str) -> None:
    print(f"[judgment] {msg}", file=sys.stderr)


def short(h: str) -> str:
    return h[:8]


def _restore_checkboxes(content: str, expected: dict[str, bool]) -> tuple[str, int]:
    """Override checkbox marks in agent output with expected state.

    Returns (fixed_content, count_of_overrides).
    """
    overrides = 0

    def _replacer(m: re.Match) -> str:
        nonlocal overrides
        key = checkbox_key(m.group("text"))
        if key not in expected:
            return m.group(0)
        want_checked = expected[key]
        is_checked = m.group("mark").lower() == "x"
        if want_checked == is_checked:
            return m.group(0)
        new_mark = "x" if want_checked else " "
        overrides += 1
        return f"- [{new_mark}] {m.group('text')}"

    fixed = CB_RE.sub(_replacer, content)
    return fixed, overrides


def main() -> int:
    start = time.time()
    log("post-proc START")

    # Read agent output from stdin (should be JSON)
    agent_output = sys.stdin.read().strip()
    if not agent_output:
        log("post-proc ERROR: no agent output on stdin")
        return 1

    # Parse JSON
    try:
        data = json.loads(agent_output)
    except (json.JSONDecodeError, TypeError):
        log(f"post-proc ERROR: agent output is not valid JSON ({len(agent_output)} bytes)")
        log(f"  first 200 chars: {agent_output[:200]}")
        return 1

    if not isinstance(data, dict):
        log(f"post-proc ERROR: expected JSON object, got {type(data).__name__}")
        return 1

    action = data.get("action", "")
    summary = data.get("summary", "")

    # Handle no-change action
    if action == "no-change":
        log(f"no-change: {summary}")
        # Still record state hash and clear spawn
        if STATE_MD.is_file():
            state_hash = hash_file(STATE_MD)
            JUDGED_STATE_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
            JUDGED_STATE_HASH_FILE.write_text(state_hash + "\n")
            log(f"recorded last_judged_state_hash={short(state_hash)}")
        clear_spawn(SPAWNED_FOR_FILE)
        log("cleared judgment_spawned_for_hash")
        elapsed_ms = int((time.time() - start) * 1000)
        log(f"post-proc DONE elapsed={elapsed_ms}ms")
        if summary:
            print(summary)
        return 0

    # Handle update action
    if action != "update":
        log(f"post-proc ERROR: unknown action '{action}'")
        return 1

    content = data.get("recommendations", "")

    # Sanity check: refuse to write if content is unreasonably small
    MIN_CONTENT_BYTES = 200
    if len(content) < MIN_CONTENT_BYTES:
        log(f"post-proc ERROR: recommendations too small ({len(content)} bytes < {MIN_CONTENT_BYTES}). Refusing to overwrite.")
        return 1

    # Step 1: Read CURRENT R checkboxes at write time
    current_checkboxes: dict[str, bool] = {}
    if RECS_MD.is_file():
        current_checkboxes = extract_checkboxes(RECS_MD.read_text(encoding="utf-8"))

    checked = sum(1 for v in current_checkboxes.values() if v)
    unchecked = sum(1 for v in current_checkboxes.values() if not v)
    log(f"read current R checkboxes: {checked} checked, {unchecked} unchecked")

    # Step 1b: Verify Context section is present (agent must copy it verbatim)
    if "## Context for Agent Review" not in content:
        log("post-proc ERROR: agent output missing '## Context for Agent Review' section. Refusing to overwrite.")
        # Still record hash and clear spawn so we don't loop
        if STATE_MD.is_file():
            state_hash = hash_file(STATE_MD)
            JUDGED_STATE_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
            JUDGED_STATE_HASH_FILE.write_text(state_hash + "\n")
            log(f"recorded last_judged_state_hash={short(state_hash)}")
        clear_spawn(SPAWNED_FOR_FILE)
        log("cleared judgment_spawned_for_hash")
        return 1

    # Step 1c: Re-inject completed items the agent dropped
    # The pipeline puts [x] items in Main Activities for done-today activities.
    # If the agent removed them, add them back before the Context section.
    agent_checkboxes = extract_checkboxes(content)
    missing_checked = []
    for key, is_checked in current_checkboxes.items():
        if is_checked and key not in agent_checkboxes:
            # Find the original line from current R
            current_r = RECS_MD.read_text(encoding="utf-8")
            for m in CB_RE.finditer(current_r):
                if checkbox_key(m.group("text")) == key and m.group("mark").lower() == "x":
                    missing_checked.append(m.group(0))
                    break
    if missing_checked:
        log(f"re-injecting {len(missing_checked)} completed items dropped by agent: "
            + ", ".join(missing_checked[:5]))
        # Insert before the Context section
        insert_lines = "\n".join(missing_checked)
        content = content.replace(
            "## Context for Agent Review",
            f"{insert_lines}\n\n## Context for Agent Review",
        )

    # Step 2: Override agent's checkbox marks with current R state
    if current_checkboxes:
        content, override_count = _restore_checkboxes(content, current_checkboxes)
        log(f"merged agent output with {override_count} checkbox overrides")

    # Step 2b: Staleness check - verify R hasn't been regenerated since spawn
    if JUDGMENT_RECS_HASH_FILE.is_file() and RECS_MD.is_file():
        spawn_recs_hash = JUDGMENT_RECS_HASH_FILE.read_text().strip()
        current_recs_hash = hash_file(RECS_MD)
        if spawn_recs_hash != current_recs_hash:
            log(f"STALE: R was regenerated while agent ran "
                f"(spawn={short(spawn_recs_hash)} current={short(current_recs_hash)}). "
                f"Discarding agent output and re-spawning judgment.")
            # Snapshot fresh R hash and re-spawn immediately
            JUDGMENT_RECS_HASH_FILE.write_text(current_recs_hash + "\n")
            state_hash = hash_file(STATE_MD) if STATE_MD.is_file() else ""
            record_spawn(SPAWNED_FOR_FILE, state_hash)
            proc = spawn_agent_task(
                root=REPO_ROOT,
                task_name="exercise-judgment",
                changed_files=["Exercise/State.md"],
                quiet=True,
            )
            if proc is None:
                log("re-spawn FAILED - clearing spawned_for")
                SPAWNED_FOR_FILE.unlink(missing_ok=True)
                JUDGMENT_RECS_HASH_FILE.unlink(missing_ok=True)
            else:
                log(f"re-spawned exercise-judgment PID={proc.pid}")
            return 0
    JUDGMENT_RECS_HASH_FILE.unlink(missing_ok=True)

    # Step 3: Write R atomically
    content_bytes = content.encode("utf-8")
    atomic_write(RECS_MD, content)
    log(f"wrote Recommendations.md ({len(content_bytes)} bytes)")

    # Step 4: Record last_judged_state_hash
    if STATE_MD.is_file():
        state_hash = hash_file(STATE_MD)
        JUDGED_STATE_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
        JUDGED_STATE_HASH_FILE.write_text(state_hash + "\n")
        log(f"recorded last_judged_state_hash={short(state_hash)}")

    # Step 5: Clear judgment_spawned_for_hash
    clear_spawn(SPAWNED_FOR_FILE)
    log("cleared judgment_spawned_for_hash")

    elapsed_ms = int((time.time() - start) * 1000)
    log(f"post-proc DONE elapsed={elapsed_ms}ms")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[judgment] post-proc FATAL: {exc}", file=sys.stderr)
        raise SystemExit(1)
