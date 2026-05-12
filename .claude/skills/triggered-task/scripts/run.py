#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
from __future__ import annotations

import argparse
import sys
import time

from pathlib import Path

from headless import (
    MAX_LOG_FIELD_LENGTH,
    TriggeredTaskError,
    ensure_logs_dir,
    extract_prompt_body,
    headless_agent,
    load_task_config,
    log_event,
    log_stage_end,
    log_stage_start,
    logs_root,
    run_post_processor,
    run_pre_processor,
    system_log,
)


def build_prompt_text(prompt_path: Path, changed_files: list[str] | None) -> str:
    """Build prompt text by inlining the body of the prompt file.

    Reads the prompt file and extracts everything after the YAML frontmatter,
    passing it directly via ``-p`` so the agent doesn't need a tool call to
    read its own instructions — avoiding potential tool_use/tool_result
    conversation-history mismatches.
    """
    body = extract_prompt_body(prompt_path.read_text(encoding="utf-8"))
    if changed_files:
        listing = "\n".join(f"  - {f}" for f in changed_files)
        return f"Files changed:\n{listing}\n\n{body}"
    return body


def emit(text: str, quiet: bool = False) -> None:
    if not quiet:
        print(text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--changed-files", help="JSON array of changed file paths")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    import json as _json
    changed_files: list[str] = _json.loads(args.changed_files) if args.changed_files else []

    start_time = time.monotonic()
    task_log = logs_root() / f"{args.name}.log"
    try:
        config = load_task_config(args.name)
        ensure_logs_dir()
        trigger = "file-change" if changed_files else "cron" if config.schedule else "manual"

        # Wrap logging calls to auto-include task name in every entry.
        def tlog(**fields: object) -> None:
            log_event(config.task_log, task=config.name, **fields)

        def tlog_start(phase: str, **fields: object) -> None:
            log_stage_start(config.task_log, phase, task=config.name, **fields)

        def tlog_end(phase: str, **fields: object) -> None:
            log_stage_end(config.task_log, phase, task=config.name, **fields)

        tlog(level="info", phase="start", trigger=trigger,
                  agent=config.agent, mode=config.mode,
                  pre_processor=config.pre_processor or "none",
                  post_processor=config.post_processor or "log-only",
                  **({"changed_files": changed_files} if changed_files else {}))
        log_event(system_log(), level="info", task=config.name, phase="start", trigger=trigger,
                  **({"changed_files": changed_files} if changed_files else {}))

        # Compute step labels based on which pipeline stages are present
        has_pre = bool(config.pre_processor)
        has_agent = config.agent != "none"
        has_post = bool(config.post_processor)
        steps: list[str] = ["config"]
        if has_pre:
            steps.append("pre-processor")
        if has_agent:
            steps.append("agent")
        if has_post:
            steps.append("post-processor")
        elif has_agent:
            steps.append("log")
        total = len(steps)
        step_num = 1

        emit(f"[{step_num}/{total}] Reading task config from frontmatter", args.quiet)
        emit(
            f"      agent: {config.agent} | mode: {config.mode}"
            + (f" | pre-processor: {config.pre_processor}" if has_pre else "")
            + f" | post-processor: {config.post_processor or 'log-only'}",
            args.quiet,
        )
        emit("", args.quiet)

        agent_status = "ok"
        post_processor_status = "ok"
        pre_processor_context = ""
        phase_durations: dict[str, float] = {}  # pre_processor_s, agent_s, post_processor_s
        run_summary = ""  # first line of post-processor output (or agent output)

        # --- Pre-processor ---
        if has_pre:
            step_num += 1
            emit(f"[{step_num}/{total}] Running pre-processor ({config.pre_processor})...", args.quiet)
            pre_started = time.monotonic()
            tlog_start("pre-processor")
            try:
                pre_result = run_pre_processor(config, changed_files=changed_files)
            except TriggeredTaskError:
                tlog_end(
                    "pre-processor",
                    status="error",
                    duration_s=round(time.monotonic() - pre_started, 1),
                )
                raise
            pre_duration = round(time.monotonic() - pre_started, 1)
            phase_durations["pre_processor_s"] = pre_duration
            tlog_end(
                "pre-processor",
                status="skipped" if pre_result.skip else "ok",
                duration_s=pre_duration,
                output=pre_result.output[:MAX_LOG_FIELD_LENGTH],
                stderr=pre_result.stderr[:MAX_LOG_FIELD_LENGTH] if pre_result.stderr else "",
                skip=pre_result.skip,
            )
            if pre_result.skip:
                emit("      Pre-processor returned skip=true, skipping agent.", args.quiet)
                duration_s = round(time.monotonic() - start_time, 1)
                tlog(level="info", phase="done", status="skipped",
                          message="pre-processor requested skip", duration_s=duration_s)
                log_event(system_log(), level="info", task=config.name, phase="done",
                          status="skipped", message="pre-processor requested skip", duration_s=duration_s)
                if not args.quiet:
                    print("")
                    print("Run skipped (pre-processor).")
                return 0
            pre_processor_context = pre_result.output
            if pre_processor_context and not args.quiet:
                print("      Pre-processor output:")
                for line in pre_processor_context.splitlines()[:10]:
                    print(f"        {line}")
                if len(pre_processor_context.splitlines()) > 10:
                    print("        ... (truncated)")
                print("")

        # --- Agent or post-processor-only ---
        if not has_agent:
            if not config.post_processor:
                # Pre-processor-only pipeline — nothing more to do.
                if not config.pre_processor:
                    raise TriggeredTaskError("agent: none requires a pre-processor or post-processor")
                duration_s = round(time.monotonic() - pre_started, 1)
                tlog(level="info", phase="done", status="ok", duration_s=duration_s)
                log_event(system_log(), level="info", task=config.name, phase="done", status="ok",
                          duration_s=duration_s)
                if not args.quiet:
                    emit(f"Done (pre-processor only, no agent).", args.quiet)
                return 0
            step_num += 1
            emit(f"[{step_num}/{total}] Running post-processor directly (no agent)...", args.quiet)
            # For post-processor-only with pre-processor, pass pre-processor output as input
            processor_input = pre_processor_context if pre_processor_context else None
            post_started = time.monotonic()
            tlog_start("post-processor")
            try:
                post_output = run_post_processor(config, processor_input, changed_files=changed_files)
            except TriggeredTaskError:
                tlog_end(
                    "post-processor",
                    status="error",
                    duration_s=round(time.monotonic() - post_started, 1),
                )
                raise
            post_duration = round(time.monotonic() - post_started, 1)
            phase_durations["post_processor_s"] = post_duration
            tlog_end(
                "post-processor",
                duration_s=post_duration,
                message=post_output if post_output else "",
            )
            if post_output and not args.quiet:
                for line in post_output.splitlines():
                    print(f"      {line}")
            if post_output:
                run_summary = post_output.splitlines()[0][:200]
        else:
            step_num += 1
            emit(f"[{step_num}/{total}] Executing agent...", args.quiet)
            # Build prompt, appending pre-processor context as a named field
            prompt_text = build_prompt_text(config.prompt_path, changed_files)
            if pre_processor_context:
                escaped = pre_processor_context.replace('"', '\\"')
                prompt_text = f'{prompt_text}\n\npre-processor="{escaped}"'
            agent_started = time.monotonic()
            tlog_start("agent")
            try:
                result = headless_agent(config, prompt_text, stream=not args.quiet)
            except TriggeredTaskError:
                tlog_end(
                    "agent",
                    status="error",
                    duration_s=round(time.monotonic() - agent_started, 1),
                )
                raise
            agent_duration = round(time.monotonic() - agent_started, 1)
            phase_durations["agent_s"] = agent_duration
            output = result.output
            usage_fields = {
                key: value
                for key, value in {
                    "model": result.model,
                    "tokens_in": result.tokens_in,
                    "tokens_out": result.tokens_out,
                    "tokens_cached": result.tokens_cached,
                    "premium_requests": result.premium_requests,
                    "cost_usd": result.cost_usd,
                    "transcript_path": result.transcript_path,
                    "structured_output": result.structured_output,
                }.items()
                if value is not None
            }
            tlog_end(
                "agent",
                duration_s=agent_duration,
                output=output[:MAX_LOG_FIELD_LENGTH],
                **usage_fields,
            )
            if args.quiet:
                print(f"Agent output:\n{output}")
            else:
                print("      Agent output:")
                lines = output.splitlines()
                for line in lines[:20]:
                    print(line)
                if len(lines) > 20:
                    print("      ... (truncated)")
                print("")

            if config.post_processor:
                step_num += 1
                emit(f"[{step_num}/{total}] Post-processor result:", args.quiet)
                post_started = time.monotonic()
                tlog_start("post-processor")
                try:
                    post_output = run_post_processor(config, output, changed_files=changed_files)
                except TriggeredTaskError:
                    tlog_end(
                        "post-processor",
                        status="error",
                        duration_s=round(time.monotonic() - post_started, 1),
                    )
                    raise
                post_duration = round(time.monotonic() - post_started, 1)
                phase_durations["post_processor_s"] = post_duration
                tlog_end(
                    "post-processor",
                    duration_s=post_duration,
                    message=post_output if post_output else "",
                )
                if post_output:
                    if args.quiet:
                        print(post_output)
                    else:
                        for line in post_output.splitlines():
                            print(f"      {line}")
                    run_summary = post_output.splitlines()[0][:200]
            else:
                step_num += 1
                emit(f"[{step_num}/{total}] Output logged", args.quiet)
                if output:
                    run_summary = output.splitlines()[0][:200]

        duration_s = round(time.monotonic() - start_time, 1)
        done_extra = {**phase_durations}
        if run_summary:
            done_extra["summary"] = run_summary
        tlog(level="info", phase="done", status="ok", duration_s=duration_s, **done_extra)
        log_event(system_log(), level="info", task=config.name, phase="done", status="ok",
                  agent=agent_status, post_processor=post_processor_status, duration_s=duration_s,
                  **done_extra)

        if not args.quiet:
            print("")
            print("Run complete.")
        return 0
    except TriggeredTaskError as exc:
        duration_s = round(time.monotonic() - start_time, 1)
        # Classify the error for structured triage by self-healing agents.
        # Use startswith() for handler/pre-processor to avoid false matches
        # when the other keyword appears in the stderr/traceback detail.
        msg = str(exc)
        if "timed out" in msg:
            error_category = "timeout"
        elif "command not found" in msg:
            error_category = "cli_crash"
        elif "parse JSON" in msg.lower():
            error_category = "output_parse_error"
        elif msg.startswith("handler failed"):
            error_category = "handler_crash"
        elif msg.startswith("pre-processor failed"):
            error_category = "pre_processor_crash"
        else:
            error_category = "agent_error"
        log_event(task_log, task=args.name,
                  level="error", phase="done", status="error", message=msg,
                  error_category=error_category, duration_s=duration_s, **phase_durations)
        log_event(system_log(), level="error", task=args.name, phase="done", status="error",
                  message=msg, error_category=error_category, duration_s=duration_s, **phase_durations)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
