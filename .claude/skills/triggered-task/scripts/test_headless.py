#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
"""Unit tests for headless.py — covers gaps the smoketest can't reach.

Run from the scripts directory:
  python3 test_headless.py -v

Focuses on: frontmatter error handling, JSON extraction edge cases,
cron install/remove cycle, write-mode flag building, transcript capture,
MCP resolution, error categories, stderr capture, and copilot output filtering.
The end-to-end smoketest covers the happy path; these cover the edges.
"""
from __future__ import annotations

from collections.abc import Callable
import json
import os
import signal
import subprocess
import tempfile
import textwrap
import types
import unittest
from unittest import mock
from pathlib import Path

# Ensure the scripts directory is importable
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

import activate
import headless
from headless import (
    COPILOT_OUTPUT_MAX_LINES,
    MAX_LOG_FIELD_LENGTH,
    TaskConfig,
    TriggeredTaskError,
    build_agent_command,
    build_agent_flags,
    cron_is_active,
    current_crontab_lines,
    extract_first_json_value,
    extract_frontmatter,
    filtered_copilot_output,
    install_crontab,
    list_tasks,
    load_task_config,
    log_event,
    normalize_agent_output,
    parse_frontmatter,
    remove_cron_entries,
    resolve_mcp,
)


# ---------------------------------------------------------------------------
# Frontmatter parsing — error cases the smoketest can't exercise
# ---------------------------------------------------------------------------
class TestFrontmatterErrors(unittest.TestCase):
    def _path(self) -> Path:
        return Path("test.md")

    def test_missing_frontmatter_raises(self) -> None:
        with self.assertRaises(TriggeredTaskError):
            extract_frontmatter("No frontmatter here", self._path())

    def test_unterminated_frontmatter_raises(self) -> None:
        with self.assertRaises(TriggeredTaskError):
            extract_frontmatter("---\nagent: claude\nmode: plan\n", self._path())

    def test_non_mapping_frontmatter_raises(self) -> None:
        with self.assertRaises(TriggeredTaskError):
            extract_frontmatter("---\n- item1\n- item2\n---\n", self._path())

    def test_empty_frontmatter_returns_empty_dict(self) -> None:
        data = extract_frontmatter("---\n---\n# Body\n", self._path())
        self.assertEqual(data, {})

    def test_nonexistent_file_raises(self) -> None:
        with self.assertRaises(TriggeredTaskError):
            parse_frontmatter("/tmp/nonexistent-prompt-file.md")

    def test_invalid_mcps_type_raises(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, dir=tempfile.gettempdir()
        ) as f:
            f.write("---\nmcps: 42\nschedule: '0 * * * *'\n---\n")
            f.flush()
            try:
                with self.assertRaises(TriggeredTaskError):
                    parse_frontmatter(f.name)
            finally:
                os.unlink(f.name)

    def test_invalid_env_type_raises(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, dir=tempfile.gettempdir()
        ) as f:
            f.write("---\nenv: not-a-dict\nschedule: '0 * * * *'\n---\n")
            f.flush()
            try:
                with self.assertRaises(TriggeredTaskError):
                    parse_frontmatter(f.name)
            finally:
                os.unlink(f.name)

    def test_missing_schedule_and_watch_raises(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, dir=tempfile.gettempdir()
        ) as f:
            f.write("---\nagent: claude\n---\n# Test\n")
            f.flush()
            try:
                config = parse_frontmatter(f.name)
                with self.assertRaises(TriggeredTaskError):
                    _ = config.task_type
            finally:
                os.unlink(f.name)


# ---------------------------------------------------------------------------
# Frontmatter parsing — defaults and coercion
# ---------------------------------------------------------------------------
class TestFrontmatterDefaults(unittest.TestCase):
    def test_defaults_when_minimal(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, dir=tempfile.gettempdir()
        ) as f:
            f.write("---\nschedule: '0 * * * *'\n---\n# Test\n")
            f.flush()
            try:
                config = parse_frontmatter(f.name)
                self.assertEqual(config.agent, "agency copilot")
                self.assertEqual(config.mode, "plan")
                self.assertIsNone(config.model)
                self.assertIsNone(config.post_processor)
                self.assertFalse(config.transcript)
            finally:
                os.unlink(f.name)

    def test_transcript_flag_parsed(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, dir=tempfile.gettempdir()
        ) as f:
            f.write("---\nschedule: '0 * * * *'\ntranscript: true\n---\n# Test\n")
            f.flush()
            try:
                config = parse_frontmatter(f.name)
                self.assertTrue(config.transcript)
            finally:
                os.unlink(f.name)

    def test_mcps_string_coerced_to_list(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, dir=tempfile.gettempdir()
        ) as f:
            f.write("---\nmcps: single-mcp\nschedule: '0 * * * *'\n---\n")
            f.flush()
            try:
                config = parse_frontmatter(f.name)
                self.assertEqual(config.mcps, ["single-mcp"])
            finally:
                os.unlink(f.name)

    def test_allow_tools_string_coerced_to_list(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, dir=tempfile.gettempdir()
        ) as f:
            f.write("---\nallow-tools: shell\nschedule: '0 * * * *'\n---\n")
            f.flush()
            try:
                config = parse_frontmatter(f.name)
                self.assertEqual(config.allow_tools, ["shell"])
            finally:
                os.unlink(f.name)

    def test_task_lookup_uses_agents_root(self) -> None:
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as tmpdir:
            root = Path(tmpdir)
            agents_dir = root / "Agents"
            agents_dir.mkdir()
            (agents_dir / "alpha.md").write_text(
                "---\nschedule: '0 * * * *'\n---\n# Alpha\n",
                encoding="utf-8",
            )
            (agents_dir / "_index_.md").write_text("# Index\n", encoding="utf-8")

            original_root = headless._cached_repo_root
            headless._cached_repo_root = root
            try:
                self.assertEqual(list_tasks(), ["alpha"])
                config = load_task_config("alpha")
                self.assertEqual(config.prompt_reference, "Agents/alpha.md")
            finally:
                headless._cached_repo_root = original_root


class TestMcpResolution(unittest.TestCase):
    """Verify resolve_mcp handles all transport types correctly."""

    def _setup_mcp_json(self, tmpdir: str, servers: dict) -> Path:
        root = Path(tmpdir)
        vscode_dir = root / ".vscode"
        vscode_dir.mkdir(exist_ok=True)
        (vscode_dir / "mcp.json").write_text(
            json.dumps({"servers": servers}),
            encoding="utf-8",
        )
        return root

    def test_workspace_workiq_server_resolves_to_agency_npx_proxy_format(self) -> None:
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as tmpdir:
            root = self._setup_mcp_json(tmpdir, {
                "workiq": {
                    "command": "npx",
                    "args": ["-y", "@microsoft/workiq", "mcp"],
                },
            })
            original_root = headless._cached_repo_root
            headless._cached_repo_root = root
            try:
                resolved = resolve_mcp("workiq", agent="agency copilot")
            finally:
                headless._cached_repo_root = original_root
        self.assertEqual(
            resolved.flag,
            "npx --package @microsoft/workiq --transport stdio -- mcp",
        )

    def test_http_mcp_resolves_to_agency_remote_proxy(self) -> None:
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as tmpdir:
            root = self._setup_mcp_json(tmpdir, {
                "my-http-mcp": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "oauthClientId": "test-client-id",
                },
            })
            original_root = headless._cached_repo_root
            headless._cached_repo_root = root
            try:
                resolved = resolve_mcp("my-http-mcp", agent="agency claude")
            finally:
                headless._cached_repo_root = original_root
        self.assertEqual(
            resolved.flag,
            "remote --url https://example.com/mcp --entra-client-id test-client-id",
        )

    def test_http_resolution_without_oauth(self) -> None:
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as tmpdir:
            root = self._setup_mcp_json(tmpdir, {
                "simple-http": {
                    "type": "http",
                    "url": "https://api.example.com/mcp",
                },
            })
            original_root = headless._cached_repo_root
            headless._cached_repo_root = root
            try:
                resolved = resolve_mcp("simple-http")
            finally:
                headless._cached_repo_root = original_root
            self.assertEqual(resolved.flag, "remote --url https://api.example.com/mcp")
            self.assertNotIn("entra", resolved.flag)

    def test_unknown_server_returns_name_as_flag(self) -> None:
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as tmpdir:
            root = self._setup_mcp_json(tmpdir, {})
            original_root = headless._cached_repo_root
            headless._cached_repo_root = root
            try:
                resolved = resolve_mcp("nonexistent-server")
            finally:
                headless._cached_repo_root = original_root
            self.assertEqual(resolved.flag, "nonexistent-server")

    def test_env_vars_propagated(self) -> None:
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as tmpdir:
            root = self._setup_mcp_json(tmpdir, {
                "env-mcp": {
                    "command": "npx",
                    "args": ["-y", "@test/env-mcp"],
                    "env": {"API_KEY": "secret123"},
                },
            })
            original_root = headless._cached_repo_root
            headless._cached_repo_root = root
            try:
                resolved = resolve_mcp("env-mcp")
            finally:
                headless._cached_repo_root = original_root
            self.assertEqual(resolved.env.get("API_KEY"), "secret123")


# ---------------------------------------------------------------------------
# JSON extraction — edge cases critical for handler pipeline
# ---------------------------------------------------------------------------
class TestJsonExtraction(unittest.TestCase):
    def test_bare_object(self) -> None:
        result = extract_first_json_value('{"files":[],"summary":"test"}')
        self.assertEqual(json.loads(result), {"files": [], "summary": "test"})

    def test_fenced_json(self) -> None:
        text = 'Some text\n```json\n{"key": "value"}\n```\nMore text'
        self.assertEqual(json.loads(extract_first_json_value(text)), {"key": "value"})

    def test_fenced_takes_priority_over_bare(self) -> None:
        text = '{"outer": 1}\n```json\n{"inner": 2}\n```'
        self.assertEqual(json.loads(extract_first_json_value(text)), {"inner": 2})

    def test_json_with_prefix_text(self) -> None:
        result = extract_first_json_value('Here is the output:\n{"result": 42}')
        self.assertEqual(json.loads(result), {"result": 42})

    def test_invalid_json_skipped(self) -> None:
        result = extract_first_json_value('{invalid} {"valid": true}')
        self.assertEqual(json.loads(result), {"valid": True})

    def test_no_json_returns_empty(self) -> None:
        self.assertEqual(extract_first_json_value("no json here"), "")


# ---------------------------------------------------------------------------
# Agent flags — write mode (smoketest only tests plan mode)
# ---------------------------------------------------------------------------
class TestWriteModeFlags(unittest.TestCase):
    def test_copilot_plan_denies_shell_and_write_by_default(self) -> None:
        flags = build_agent_flags("copilot", "plan")
        self.assertIn("--deny-tool", flags)
        self.assertIn("shell", flags)
        self.assertIn("write", flags)

    def test_copilot_plan_can_allow_shell(self) -> None:
        flags = build_agent_flags("copilot", "plan", ["shell"])
        self.assertIn("--allow-tool", flags)
        self.assertIn("shell", flags)
        self.assertNotIn("write", [flags[index + 1] for index, value in enumerate(flags[:-1]) if value == "--allow-tool"])

    def test_claude_write(self) -> None:
        flags = build_agent_flags("claude", "write")
        self.assertIn("--dangerously-skip-permissions", flags)
        self.assertNotIn("--permission-mode", flags)

    def test_copilot_write(self) -> None:
        flags = build_agent_flags("copilot", "write")
        self.assertIn("--allow-all-tools", flags)
        self.assertNotIn("--deny-tool", flags)

    def test_unknown_agent_raises(self) -> None:
        with self.assertRaises(TriggeredTaskError):
            build_agent_flags("unknown-agent", "plan")


class TestBuildAgentCommand(unittest.TestCase):
    def _config(
        self,
        agent: str,
        *,
        mcps: list[str] | None = None,
        requested_mcps: list[str] | None = None,
    ) -> TaskConfig:
        return TaskConfig(
            name="test-task",
            prompt_path=Path("/tmp/test-task.md"),
            agent=agent,
            mode="plan",
            schedule="0 * * * *",
            mcps=mcps or [],
            requested_mcps=requested_mcps,
        )

    def test_copilot_disables_custom_instructions(self) -> None:
        command = build_agent_command(self._config("agency copilot"), "prompt")
        self.assertIn("--no-ask-user", command)
        self.assertIn("--no-custom-instructions", command)

    def test_copilot_passes_allow_tool_flags(self) -> None:
        config = self._config("agency copilot")
        config = TaskConfig(**{**config.__dict__, "allow_tools": ["shell"]})
        command = build_agent_command(config, "prompt")
        self.assertIn("--allow-tool", command)
        allow_targets = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--allow-tool"
        ]
        self.assertEqual(allow_targets, ["shell"])

    def test_copilot_uses_optional_mcp_flags_when_supported(self) -> None:
        with mock.patch.object(headless, "agent_supported_flags", return_value=frozenset({"--no-default-mcps", "--disable-mcp-server"})):
            with mock.patch.object(headless, "workspace_mcp_server_names", return_value=["workiq", "softeria-ms365"]):
                command = build_agent_command(
                    self._config("agency copilot", mcps=["workiq"]),
                    "prompt",
                )
        self.assertIn("--no-default-mcps", command)
        self.assertIn("--disable-mcp-server", command)
        disable_targets = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--disable-mcp-server"
        ]
        self.assertEqual(disable_targets, ["softeria-ms365"])

    def test_copilot_skips_optional_mcp_flags_when_unsupported(self) -> None:
        with mock.patch.object(headless, "agent_supported_flags", return_value=frozenset()):
            with mock.patch.object(headless, "workspace_mcp_server_names", return_value=["workiq", "softeria-ms365"]):
                command = build_agent_command(
                    self._config("agency copilot", mcps=["workiq"]),
                    "prompt",
                )
        self.assertNotIn("--no-default-mcps", command)
        self.assertNotIn("--disable-mcp-server", command)

    def test_copilot_checks_supported_flags_once_per_command(self) -> None:
        with mock.patch.object(
            headless,
            "agent_supported_flags",
            return_value=frozenset({"--no-default-mcps", "--disable-mcp-server"}),
        ) as supported_flags:
            with mock.patch.object(headless, "workspace_mcp_server_names", return_value=[]):
                build_agent_command(self._config("agency copilot"), "prompt")
        supported_flags.assert_called_once_with("agency copilot")

    def test_copilot_uses_requested_mcp_names_for_resolved_config(self) -> None:
        with mock.patch.object(headless, "agent_supported_flags", return_value=frozenset({"--disable-mcp-server"})):
            with mock.patch.object(headless, "workspace_mcp_server_names", return_value=["workiq", "softeria-ms365"]):
                command = build_agent_command(
                    self._config(
                        "agency copilot",
                        mcps=["npx --package @acme/workiq-mcp --transport stdio"],
                        requested_mcps=["workiq"],
                    ),
                    "prompt",
                )
        disable_targets = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "--disable-mcp-server"
        ]
        self.assertEqual(disable_targets, ["softeria-ms365"])

    def test_claude_keeps_system_prompt_path(self) -> None:
        command = build_agent_command(self._config("claude"), "prompt")
        self.assertIn("--append-system-prompt", command)
        self.assertNotIn("--no-custom-instructions", command)

    def test_copilot_transcript_adds_share_when_supported(self) -> None:
        config = TaskConfig(
            name="test-task",
            prompt_path=Path("/tmp/test-task.md"),
            agent="agency copilot",
            mode="plan",
            schedule="0 * * * *",
            transcript=True,
        )
        with mock.patch.object(headless, "agent_supported_flags", return_value=frozenset({"--share"})):
            with mock.patch.object(headless, "workspace_mcp_server_names", return_value=[]):
                with mock.patch.object(headless, "ensure_logs_dir"):
                    command = build_agent_command(config, "prompt")
        self.assertIn("--share", command)
        share_index = command.index("--share")
        self.assertTrue(command[share_index + 1].endswith("-transcript.md"))

    def test_copilot_no_share_when_transcript_false(self) -> None:
        config = self._config("agency copilot")
        with mock.patch.object(headless, "agent_supported_flags", return_value=frozenset({"--share"})):
            with mock.patch.object(headless, "workspace_mcp_server_names", return_value=[]):
                command = build_agent_command(config, "prompt")
        self.assertNotIn("--share", command)

    def test_copilot_no_share_when_flag_unsupported(self) -> None:
        config = TaskConfig(
            name="test-task",
            prompt_path=Path("/tmp/test-task.md"),
            agent="agency copilot",
            mode="plan",
            schedule="0 * * * *",
            transcript=True,
        )
        with mock.patch.object(headless, "agent_supported_flags", return_value=frozenset()):
            with mock.patch.object(headless, "workspace_mcp_server_names", return_value=[]):
                command = build_agent_command(config, "prompt")
        self.assertNotIn("--share", command)

    def test_claude_ignores_transcript_flag(self) -> None:
        config = TaskConfig(
            name="test-task",
            prompt_path=Path("/tmp/test-task.md"),
            agent="claude",
            mode="plan",
            schedule="0 * * * *",
            transcript=True,
        )
        command = build_agent_command(config, "prompt")
        self.assertNotIn("--share", command)


class TestAgentSupportedFlags(unittest.TestCase):
    def setUp(self) -> None:
        headless.agent_supported_flags.cache_clear()

    def tearDown(self) -> None:
        headless.agent_supported_flags.cache_clear()

    def test_times_out_to_empty_flag_set(self) -> None:
        with mock.patch.object(
            headless.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["copilot", "--help"], timeout=1),
        ) as run_mock:
            self.assertEqual(headless.agent_supported_flags("copilot"), frozenset())
        self.assertEqual(run_mock.call_args.kwargs["timeout"], headless.AGENT_HELP_TIMEOUT)


# ---------------------------------------------------------------------------
# Cron management — the gap the issue specifically called out
# ---------------------------------------------------------------------------
class TestCronCycle(unittest.TestCase):
    """Test cron install/remove/check. Saves and restores original crontab."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            result = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True, check=False
            )
            cls._original_crontab = result.stdout if result.returncode == 0 else None
        except FileNotFoundError:
            cls._original_crontab = None

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._original_crontab is not None:
            subprocess.run(
                ["crontab", "-"], input=cls._original_crontab,
                text=True, capture_output=True, check=False,
            )
        else:
            subprocess.run(["crontab", "-r"], capture_output=True, check=False)

    def test_install_check_remove_cycle(self) -> None:
        test_name = "unittest-cron-test-xyzzy"
        test_line = f"0 0 * * * echo {test_name}"

        lines = [l for l in current_crontab_lines() if test_name not in l]
        lines.append(test_line)
        install_crontab(lines)
        self.assertTrue(cron_is_active(test_name))

        removed = remove_cron_entries(test_name)
        self.assertTrue(removed)
        self.assertFalse(cron_is_active(test_name))

    def test_remove_nonexistent_returns_false(self) -> None:
        self.assertFalse(remove_cron_entries("nonexistent-task-xyzzy-12345"))


# ---------------------------------------------------------------------------
# Log event — verify JSONL format (one test, not a whole class)
# ---------------------------------------------------------------------------
class TestLogEvent(unittest.TestCase):
    def test_writes_valid_jsonl(self) -> None:
        with tempfile.NamedTemporaryFile(
            suffix=".log", delete=False, dir=tempfile.gettempdir()
        ) as f:
            log_path = Path(f.name)
        try:
            log_event(log_path, level="info", phase="test", message="hello")
            line = json.loads(log_path.read_text().strip())
            self.assertEqual(line["level"], "info")
            self.assertIn("ts", line)
        finally:
            log_path.unlink(missing_ok=True)


class TestActivateWatchLoop(unittest.TestCase):
    def test_sigterm_shutdown_exits_cleanly_without_error_log(self) -> None:
        config = TaskConfig(
            name="watcher-task",
            prompt_path=Path("/tmp/watcher-task.md"),
            agent="none",
            mode="plan",
            watch_path=["Notes"],
        )

        class DummyStdout:
            def fileno(self) -> int:
                return 123

            def read(self, _size: int) -> str:
                return ""

        class DummyStderr:
            def read(self) -> str:
                return ""

        class DummyProcess:
            def __init__(self) -> None:
                self.stdout = DummyStdout()
                self.stderr = DummyStderr()
                self.terminate_calls = 0

            def poll(self) -> int | None:
                return -signal.SIGTERM if self.terminate_calls else None

            def terminate(self) -> None:
                self.terminate_calls += 1

        handlers: dict[int, Callable[[int, types.FrameType | None], None]] = {}
        process = DummyProcess()
        triggered_shutdown = False

        def fake_signal(signum: int, handler: Callable[[int, types.FrameType | None], None]) -> None:
            handlers[signum] = handler

        def fake_select(read_fds: list[int], _write: list[int], _error: list[int], _timeout: float | None = None) -> tuple[list[int], list[int], list[int]]:
            nonlocal triggered_shutdown
            if not triggered_shutdown:
                triggered_shutdown = True
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            return ([read_fds[0]], [], [])

        fake_fcntl = types.SimpleNamespace(
            F_GETFL=1,
            F_SETFL=2,
            fcntl=lambda _fd, _op, _arg=None: 0,
        )
        fake_atexit = types.SimpleNamespace(register=lambda _fn: None)
        original_stderr = activate.sys.stderr

        with (
            mock.patch("activate.load_task_config", return_value=config),
            mock.patch("activate._validate_watcher_prereqs", return_value=[Path("/tmp")]),
            mock.patch("activate.ensure_logs_dir"),
            mock.patch("headless.repo_root", return_value=Path("/tmp")),
            mock.patch("activate.log_event") as log_mock,
            mock.patch("activate.repo_root", return_value=Path("/tmp")),
            mock.patch("activate.subprocess.Popen", return_value=process),
            mock.patch("activate.signal.signal", side_effect=fake_signal),
            mock.patch.dict(
                sys.modules,
                {
                    "atexit": fake_atexit,
                    "fcntl": fake_fcntl,
                    "select": types.SimpleNamespace(select=fake_select),
                },
                clear=False,
            ),
        ):
            result = activate.watch_loop(config.name)

        self.assertEqual(result, 0)
        self.assertIs(activate.sys.stderr, original_stderr)
        self.assertGreaterEqual(process.terminate_calls, 1)
        error_messages = [
            call.kwargs.get("message", "")
            for call in log_mock.call_args_list
            if call.kwargs.get("level") == "error"
        ]
        self.assertFalse(any("inotifywait exited with status" in message for message in error_messages))


class TestHeadlessAgentBehavior(unittest.TestCase):
    def _config(self, **kwargs: object) -> TaskConfig:
        defaults = {
            "name": "test-task",
            "prompt_path": Path("/tmp/test-task.md"),
            "agent": "claude",
            "mode": "plan",
            "schedule": "0 * * * *",
        }
        defaults.update(kwargs)
        return TaskConfig(**defaults)

    def test_retries_empty_output_once(self) -> None:
        config = self._config()
        first = mock.Mock(returncode=0, stdout="", stderr="")
        second = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")

        with (
            mock.patch("headless.resolve_task_config", side_effect=lambda value: value),
            mock.patch("headless.build_agent_env", return_value={}),
            mock.patch("headless.build_agent_command", return_value=["claude", "-p", "prompt"]),
            mock.patch("headless.subprocess.run", side_effect=[first, second]) as run_mock,
            mock.patch("headless.time.sleep") as sleep_mock,
            mock.patch("headless.log_event") as log_mock,
        ):
            result = headless.headless_agent(config, "prompt")

        self.assertEqual(json.loads(result.output), {"ok": True})
        self.assertEqual(run_mock.call_count, 2)
        sleep_mock.assert_called_once()
        retry_messages = [call.kwargs.get("message", "") for call in log_mock.call_args_list]
        self.assertTrue(any("retrying" in message for message in retry_messages))

    def test_raises_before_post_processor_when_json_parse_fails(self) -> None:
        config = self._config(post_processor="write-files.sh")
        completed = mock.Mock(returncode=0, stdout="not json", stderr="")

        with (
            mock.patch("headless.resolve_task_config", side_effect=lambda value: value),
            mock.patch("headless.build_agent_env", return_value={}),
            mock.patch("headless.build_agent_command", return_value=["claude", "-p", "prompt"]),
            mock.patch("headless.subprocess.run", return_value=completed),
            mock.patch("headless.log_event") as log_mock,
        ):
            with self.assertRaisesRegex(
                TriggeredTaskError,
                "failed to parse JSON from agent output for post-processor",
            ):
                headless.headless_agent(config, "prompt")

        messages = [call.kwargs.get("message", "") for call in log_mock.call_args_list]
        self.assertIn("failed to parse JSON from agent output; raw output follows", messages)
        self.assertIn("[raw-output] not json", messages)

    def test_allows_plain_text_without_post_processor(self) -> None:
        config = self._config()
        completed = mock.Mock(returncode=0, stdout="plain text", stderr="")

        with (
            mock.patch("headless.resolve_task_config", side_effect=lambda value: value),
            mock.patch("headless.build_agent_env", return_value={}),
            mock.patch("headless.build_agent_command", return_value=["claude", "-p", "prompt"]),
            mock.patch("headless.subprocess.run", return_value=completed),
            mock.patch("headless.log_event"),
        ):
            result = headless.headless_agent(config, "prompt")

        self.assertEqual(result.output, "plain text")
        self.assertIsNone(result.structured_output)


# ---------------------------------------------------------------------------
# Error categories — structured triage for self-healing
# ---------------------------------------------------------------------------
class TestErrorCategories(unittest.TestCase):
    """Verify error_category field is written to JSONL log entries."""

    def test_log_event_with_error_category(self) -> None:
        with tempfile.NamedTemporaryFile(
            suffix=".log", delete=False, dir=tempfile.gettempdir()
        ) as f:
            log_path = Path(f.name)
        try:
            log_event(log_path, level="error", phase="agent",
                      message="timed out", error_category="timeout")
            line = json.loads(log_path.read_text().strip())
            self.assertEqual(line["error_category"], "timeout")
        finally:
            log_path.unlink(missing_ok=True)

    def test_log_event_without_error_category(self) -> None:
        with tempfile.NamedTemporaryFile(
            suffix=".log", delete=False, dir=tempfile.gettempdir()
        ) as f:
            log_path = Path(f.name)
        try:
            log_event(log_path, level="info", phase="agent", message="ok")
            line = json.loads(log_path.read_text().strip())
            self.assertNotIn("error_category", line)
        finally:
            log_path.unlink(missing_ok=True)

    def test_normalize_agent_output_logs_parse_error_category(self) -> None:
        config = TaskConfig(
            name="parse-test", prompt_path=Path("/tmp/parse-test.md"),
            agent="claude", mode="plan", schedule="0 * * * *",
            post_processor="write-files.sh",
        )
        original = headless._cached_repo_root
        headless._cached_repo_root = Path(tempfile.gettempdir())
        try:
            logs_dir = Path(tempfile.gettempdir()) / "Agents" / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            task_log = logs_dir / "parse-test.log"
            normalize_agent_output(config, "not json output")
            if task_log.is_file():
                entries = [json.loads(line) for line in task_log.read_text().strip().splitlines()]
                categories = [e.get("error_category") for e in entries if e.get("error_category")]
                self.assertIn("output_parse_error", categories)
        finally:
            headless._cached_repo_root = original
            task_log = Path(tempfile.gettempdir()) / "Agents" / "logs" / "parse-test.log"
            if task_log.is_file():
                task_log.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Stderr capture — full stderr, not truncated at 500 chars
# ---------------------------------------------------------------------------
class TestStderrCapture(unittest.TestCase):
    """Verify stderr is logged up to MAX_LOG_FIELD_LENGTH, not 500 chars."""

    def test_max_log_field_length_is_reasonable(self) -> None:
        self.assertGreaterEqual(MAX_LOG_FIELD_LENGTH, 2000)

    def test_long_stderr_not_truncated_in_log(self) -> None:
        long_stderr = "x" * 1500
        with tempfile.NamedTemporaryFile(
            suffix=".log", delete=False, dir=tempfile.gettempdir()
        ) as f:
            log_path = Path(f.name)
        try:
            log_event(log_path, phase="agent", level="info",
                      message=long_stderr[:MAX_LOG_FIELD_LENGTH])
            line = json.loads(log_path.read_text().strip())
            self.assertEqual(len(line["message"]), 1500)
        finally:
            log_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Filtered copilot output — configurable line limit
# ---------------------------------------------------------------------------
class TestFilteredCopilotOutput(unittest.TestCase):
    """Verify the copilot output filter uses the configurable limit."""

    def test_max_lines_is_100(self) -> None:
        self.assertEqual(COPILOT_OUTPUT_MAX_LINES, 100)

    def test_50_lines_all_kept(self) -> None:
        lines = [f"Line {i}" for i in range(50)]
        result = filtered_copilot_output("\n".join(lines))
        self.assertEqual(len(result.splitlines()), 50)

    def test_150_lines_capped_at_max(self) -> None:
        lines = [f"Line {i}" for i in range(150)]
        result = filtered_copilot_output("\n".join(lines))
        self.assertEqual(len(result.splitlines()), COPILOT_OUTPUT_MAX_LINES)

    def test_noise_lines_still_filtered(self) -> None:
        lines = ["● some noise", "│ more noise", "Real content line 1", "Real content line 2"]
        result = filtered_copilot_output("\n".join(lines))
        self.assertNotIn("●", result)
        self.assertIn("Real content line 1", result)


# ---------------------------------------------------------------------------
# AgentResult — transcript_path field
# ---------------------------------------------------------------------------
class TestAgentResultTranscript(unittest.TestCase):
    """Verify AgentResult dataclass supports transcript_path."""

    def test_default_transcript_path_is_none(self) -> None:
        result = headless.AgentResult(output="test", stderr="")
        self.assertIsNone(result.transcript_path)

    def test_transcript_path_can_be_set(self) -> None:
        result = headless.AgentResult(
            output="test", stderr="",
            transcript_path="/logs/test-transcript.md",
        )
        self.assertEqual(result.transcript_path, "/logs/test-transcript.md")


if __name__ == "__main__":
    unittest.main()
