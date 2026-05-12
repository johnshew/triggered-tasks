#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import shutil
import sys
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

HEADLESS_PROMPT = (
    "You are a headless automated agent. Output ONLY what the prompt asks for. "
    "Never explain, never ask questions, never add commentary."
)
HEADLESS_TIMEOUT = 120
HEADLESS_EMPTY_OUTPUT_RETRIES = 2
HEADLESS_EMPTY_OUTPUT_RETRY_DELAY_S = 2.0
HEADLESS_TIMEOUT_RETRIES = 1
AGENT_HELP_TIMEOUT = 10.0
ANSI_RE = re.compile(
    r"\x1b"          # ESC
    r"(?:"
    r"\[[0-9;?]*[A-Za-z]"   # CSI sequences (including ? for DEC private modes)
    r"|"
    r"\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences (terminated by BEL or ST)
    r"|"
    r"[()][0-9A-Za-z]"      # Character set designation
    r"|"
    r"[=>NOMDEHcZ78]"       # Simple two-char sequences
    r")"
)
COPILOT_NOISE_PREFIXES = (
    "●",
    "  │",
    "  └",
    "✗",
    "Script ",
    "Total usage",
    "API time",
    "Total session",
    "Total code",
    "Breakdown",
    " claude-",
    "🤖",
    "📁",
    "📦",
    "🧠",
    "✅",
    "╔",
    "║",
    "╠",
    "╚",
)
# Visible task prompts live directly under Agents/; `_index_.md` is auto-generated.
EXCLUDED_TASK_FILE_NAMES = {"_index_.md"}
MAX_RAW_OUTPUT_LOG_LENGTH = 20_000
MAX_LOG_FIELD_LENGTH = MAX_RAW_OUTPUT_LOG_LENGTH
COPILOT_OUTPUT_MAX_LINES = 100
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPO_ROOT = SCRIPT_DIR.parents[3]

_cached_repo_root: Path | None = None


def _extract_traceback(stderr: str) -> str | None:
    """Return the last Python traceback from stderr, or None."""
    marker = "Traceback (most recent call last):"
    idx = stderr.rfind(marker)
    if idx == -1:
        return None
    return stderr[idx:].strip()


class TriggeredTaskError(RuntimeError):
    """Base error for triggered-task failures."""


@dataclass(frozen=True)
class ResolvedMcp:
    flag: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResult:
    output: str
    stderr: str
    model: str | None = None
    tokens_in: str | None = None
    tokens_out: str | None = None
    tokens_cached: str | None = None
    premium_requests: str | None = None
    cost_usd: str | None = None
    transcript_path: str | None = None
    structured_output: dict | list | None = None


@dataclass(frozen=True)
class PreProcessorResult:
    output: str
    skip: bool = False
    stderr: str = ""


@dataclass(frozen=True)
class TaskConfig:
    name: str
    prompt_path: Path
    agent: str = "agency copilot"
    mode: str = "plan"
    model: str | None = None
    allow_tools: list[str] = field(default_factory=list)
    handler: str | None = None
    pre_processor: str | None = None
    post_processor: str | None = None
    schedule: str | None = None
    watch_path: list[str] = field(default_factory=list)
    watch_ignore: list[str] = field(default_factory=list)
    mcps: list[str] = field(default_factory=list)
    requested_mcps: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: int | None = None
    transcript: bool = True
    debounce: int | None = None  # seconds; delays watcher dispatch via systemd timer

    def _resolve_handler_path(self, name: str | None) -> Path | None:
        if not name:
            return None
        if "/" in name or "\\" in name:
            return repo_root() / name
        # Resolve bare handler names relative to the task's own directory
        return self.prompt_path.parent / "handlers" / name

    @property
    def prompt_reference(self) -> str:
        return repo_relative(self.prompt_path)

    @property
    def handler_path(self) -> Path | None:
        # post_processor takes precedence, falls back to handler for compat
        return self._resolve_handler_path(self.post_processor or self.handler)

    @property
    def handler_reference(self) -> str | None:
        if not self.handler_path:
            return None
        return repo_relative(self.handler_path)

    @property
    def pre_processor_path(self) -> Path | None:
        return self._resolve_handler_path(self.pre_processor)

    @property
    def pre_processor_reference(self) -> str | None:
        if not self.pre_processor_path:
            return None
        return repo_relative(self.pre_processor_path)

    @property
    def post_processor_path(self) -> Path | None:
        return self.handler_path  # same resolution

    @property
    def post_processor_reference(self) -> str | None:
        return self.handler_reference

    @property
    def task_log(self) -> Path:
        return logs_root() / f"{self.name}.log"

    @property
    def transcript_log(self) -> Path:
        """Path where the full session transcript is written."""
        return logs_root() / f"{self.name}-transcript.md"

    @property
    def all_watch_paths(self) -> list[str]:
        """Return watchPath as a list."""
        return self.watch_path

    def watch_path_absolute_for(self, wp: str) -> Path:
        """Resolve a single watch path string to an absolute path."""
        p = Path(wp)
        if p.is_absolute():
            return p
        return repo_root() / wp

    @property
    def watch_path_absolute(self) -> Path | None:
        """Return the first watch path as an absolute path."""
        if not self.watch_path:
            return None
        return self.watch_path_absolute_for(self.watch_path[0])

    @property
    def task_type(self) -> str:
        if self.schedule and self.watch_path:
            return "multi"
        if self.schedule:
            return "cron"
        if self.watch_path:
            return "watcher"
        raise TriggeredTaskError(
            f"task '{self.name}' has no schedule or watchPath in {self.prompt_reference}"
        )


def repo_root() -> Path:
    global _cached_repo_root
    if _cached_repo_root is not None:
        return _cached_repo_root
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            cwd=DEFAULT_REPO_ROOT,
        )
        _cached_repo_root = Path(completed.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        _cached_repo_root = DEFAULT_REPO_ROOT
    return _cached_repo_root


def repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root()))
    except ValueError:
        return str(path)


def clean_path() -> str:
    path_entries = [Path.home() / ".local" / "bin"]
    node_path = shutil.which("node")
    if not node_path:
        # Cron/headless environments lack nvm; search nvm directories directly
        candidates = sorted(
            glob.glob(str(Path.home() / ".nvm/versions/node/*/bin/node")),
            reverse=True,
        )
        for candidate in candidates:
            if os.access(candidate, os.X_OK):
                node_path = candidate
                break
    if node_path:
        path_entries.append(Path(node_path).resolve().parent)
    agency_path = shutil.which("agency")
    if not agency_path:
        # Fallback: check standard agency install location under cron's minimal PATH
        candidate = Path.home() / ".config" / "agency" / "CurrentVersion" / "agency"
        if candidate.is_file():
            agency_path = str(candidate)
    if agency_path:
        path_entries.insert(0, Path(agency_path).resolve().parent)
    path_entries.extend(Path(part) for part in ("/usr/local/bin", "/usr/bin", "/bin"))
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in path_entries:
        value = str(entry)
        if value not in seen:
            ordered.append(value)
            seen.add(value)
    return os.pathsep.join(ordered)


# ---------------------------------------------------------------------------
# Task directory resolution (multi-directory support)
# ---------------------------------------------------------------------------

TASK_DIRS_CONFIG = Path("Agents/data/config.json")


def _load_task_directories_config() -> list[str]:
    """Read additional task directories from Agents/data/config.json.

    Returns the raw list of repo-relative directory strings from the config's
    ``task_directories`` key.  Returns ``[]`` if the config file is missing or
    has no ``task_directories`` entry.
    """
    config_path = repo_root() / TASK_DIRS_CONFIG
    if not config_path.is_file():
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        dirs = data.get("task_directories", [])
        if isinstance(dirs, list):
            return [str(d) for d in dirs if d]
        return []
    except (json.JSONDecodeError, OSError):
        return []


def tasks_dir() -> Path:
    """Primary task directory (Agents/).  Used for creating new tasks."""
    return repo_root() / "Agents"


def all_task_dirs() -> list[Path]:
    """All task directories: primary (Agents/) plus any extras from config.

    The primary directory is always first.  Additional directories are listed
    in config order.  Non-existent directories are silently skipped.
    """
    root = repo_root()
    dirs: list[Path] = [root / "Agents"]
    seen = {dirs[0].resolve()}
    for rel in _load_task_directories_config():
        p = root / rel
        resolved = p.resolve()
        if resolved not in seen and p.is_dir():
            dirs.append(p)
            seen.add(resolved)
    return dirs


def logs_root() -> Path:
    return repo_root() / "Agents" / "logs"


# ---------------------------------------------------------------------------
# Desired-tasks registry
# ---------------------------------------------------------------------------

DESIRED_TASKS_PATH = Path("Agents/data/desired-tasks.json")


def _desired_tasks_file() -> Path:
    return repo_root() / DESIRED_TASKS_PATH


def _load_desired_tasks() -> dict:
    """Load the desired-tasks file. Returns {"watchers": [], "cron": []} if missing."""
    path = _desired_tasks_file()
    if not path.is_file():
        return {"watchers": [], "cron": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"watchers": data.get("watchers", []), "cron": data.get("cron", [])}
    except (json.JSONDecodeError, OSError):
        return {"watchers": [], "cron": []}


def _save_desired_tasks(desired: dict) -> None:
    """Persist the desired-tasks list to disk."""
    path = _desired_tasks_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "_comment": "Tasks that should always be running. "
                    "Maintained by activate.py/teardown.py. "
                    "The triggered-tasks-health-check cron job verifies and restarts any that have died.",
        "watchers": sorted(set(desired.get("watchers", []))),
        "cron": sorted(set(desired.get("cron", []))),
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_desired_watchers() -> list[str]:
    """Load the desired watchers list. Returns [] if the file doesn't exist."""
    return _load_desired_tasks()["watchers"]


def add_desired_watcher(name: str) -> None:
    """Add a watcher to the desired-tasks registry."""
    desired = _load_desired_tasks()
    if name not in desired["watchers"]:
        desired["watchers"].append(name)
        _save_desired_tasks(desired)


def remove_desired_watcher(name: str) -> None:
    """Remove a watcher from the desired-tasks registry."""
    desired = _load_desired_tasks()
    if name in desired["watchers"]:
        desired["watchers"].remove(name)
        _save_desired_tasks(desired)


def add_desired_cron(name: str) -> None:
    """Add a cron task to the desired-tasks registry."""
    desired = _load_desired_tasks()
    if name not in desired["cron"]:
        desired["cron"].append(name)
        _save_desired_tasks(desired)


def remove_desired_cron(name: str) -> None:
    """Remove a cron task from the desired-tasks registry."""
    desired = _load_desired_tasks()
    if name in desired["cron"]:
        desired["cron"].remove(name)
        _save_desired_tasks(desired)


def ensure_logs_dir() -> Path:
    log_dir = logs_root()
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_event(log_path: Path, **fields: Any) -> None:
    """Append a single JSONL event to a log file.

    Validates and sanitises fields:
    - String values longer than MAX_LOG_FIELD_LENGTH are truncated
      and a ``_truncated`` flag is added.
    - Caller-supplied ``ts`` is silently dropped (always generated).
    - Non-serialisable values are replaced with their ``repr()``.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fields.pop("ts", None)  # always generated, never caller-supplied
    truncated = False
    sanitised: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, str) and len(value) > MAX_LOG_FIELD_LENGTH:
            sanitised[key] = value[:MAX_LOG_FIELD_LENGTH]
            truncated = True
        else:
            sanitised[key] = value
    if truncated:
        sanitised["_truncated"] = True
    entry: dict[str, Any] = {"ts": _utc_now(), "log_schema": 3, **sanitised}
    try:
        line = json.dumps(entry, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        # Last resort: repr() the entire payload so we never lose the event
        entry = {"ts": _utc_now(), "level": "error", "phase": "log_event",
                 "message": f"non-serialisable log payload: {repr(sanitised)[:MAX_LOG_FIELD_LENGTH]}"}
        line = json.dumps(entry, separators=(",", ":"))
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def log_stage_start(log_path: Path, phase: str, **fields: Any) -> None:
    """Log a stage start event."""
    log_event(log_path, phase=phase, status="start", **fields)


def log_stage_end(log_path: Path, phase: str, *, status: str = "ok", duration_s: float | None = None,
                  **fields: Any) -> None:
    """Log a stage completion event."""
    payload: dict[str, Any] = {"phase": phase, "status": status}
    if duration_s is not None:
        payload["duration_s"] = duration_s
    payload.update(fields)
    log_event(log_path, **payload)


def system_log() -> Path:
    return logs_root() / "triggered-tasks.log"


def extract_frontmatter(text: str, prompt_path: Path) -> dict[str, Any]:
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        raise TriggeredTaskError(f"no frontmatter in {repo_relative(prompt_path)}")

    end_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break
    if end_index is None:
        raise TriggeredTaskError(f"unterminated frontmatter in {repo_relative(prompt_path)}")

    frontmatter_text = "\n".join(lines[1:end_index])
    data = yaml.safe_load(frontmatter_text) or {}
    if not isinstance(data, dict):
        raise TriggeredTaskError(f"frontmatter in {repo_relative(prompt_path)} must be a mapping")
    return data


def extract_prompt_body(text: str) -> str:
    """Return the body of a prompt file (everything after the frontmatter block).

    If the file has no valid frontmatter (missing opening ``---``, fewer than
    3 lines, or no closing ``---``), the entire text is returned stripped.
    This is intentional — the caller receives usable prompt content regardless
    of whether frontmatter is present.
    """
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return text.strip()
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1:]).strip()
    return text.strip()


def parse_frontmatter(prompt_path: str | Path) -> TaskConfig:
    prompt = Path(prompt_path)
    if not prompt.is_absolute():
        prompt = repo_root() / prompt
    if not prompt.is_file():
        raise TriggeredTaskError(f"prompt not found: {repo_relative(prompt)}")

    data = extract_frontmatter(prompt.read_text(encoding="utf-8"), prompt)
    name = prompt.stem
    agent = str(data.get("agent") or "agency copilot")
    mode = str(data.get("mode") or "plan")
    model_value = data.get("model")
    model = None if model_value in (None, "") else str(model_value)
    allow_tools_raw = data.get("allow-tools") or []
    if isinstance(allow_tools_raw, str):
        allow_tools = [allow_tools_raw]
    elif isinstance(allow_tools_raw, list):
        allow_tools = [str(item) for item in allow_tools_raw if item not in (None, "")]
    else:
        raise TriggeredTaskError(f"allow-tools must be a list in {repo_relative(prompt)}")
    handler_value = data.get("handler")
    handler = None if handler_value in (None, "") else str(handler_value)
    pre_proc_value = data.get("pre-processor")
    pre_processor = None if pre_proc_value in (None, "") else str(pre_proc_value)
    post_proc_value = data.get("post-processor")
    post_processor = None if post_proc_value in (None, "") else str(post_proc_value)
    schedule_value = data.get("schedule")
    schedule = None if schedule_value in (None, "") else str(schedule_value)
    watch_value = data.get("watchPath")
    if isinstance(watch_value, list):
        watch_path = [str(v) for v in watch_value if v not in (None, "")]
    elif watch_value in (None, ""):
        watch_path = []
    else:
        watch_path = [str(watch_value)]

    watch_ignore_raw = data.get("watchIgnore") or []
    if isinstance(watch_ignore_raw, str):
        watch_ignore = [watch_ignore_raw]
    elif isinstance(watch_ignore_raw, list):
        watch_ignore = [str(item) for item in watch_ignore_raw if item not in (None, "")]
    else:
        watch_ignore = []

    mcps_raw = data.get("mcps") or []
    if isinstance(mcps_raw, str):
        mcps = [mcps_raw]
    elif isinstance(mcps_raw, list):
        mcps = [str(item) for item in mcps_raw if item not in (None, "")]
    else:
        raise TriggeredTaskError(f"mcps must be a list in {repo_relative(prompt)}")

    env_raw = data.get("env") or {}
    if not isinstance(env_raw, dict):
        raise TriggeredTaskError(f"env must be a mapping in {repo_relative(prompt)}")
    env = {str(key): str(value) for key, value in env_raw.items()}

    timeout_raw = data.get("timeout")
    timeout = int(timeout_raw) if timeout_raw not in (None, "") else None

    transcript = bool(data.get("transcript", True))

    debounce_raw = data.get("debounce")
    debounce = int(debounce_raw) if debounce_raw not in (None, "") else None

    return TaskConfig(
        name=name,
        prompt_path=prompt,
        agent=agent,
        mode=mode,
        model=model,
        allow_tools=allow_tools,
        handler=handler,
        pre_processor=pre_processor,
        post_processor=post_processor,
        schedule=schedule,
        watch_path=watch_path,
        watch_ignore=watch_ignore,
        mcps=mcps,
        env=env,
        timeout=timeout,
        transcript=transcript,
        debounce=debounce,
    )


def load_task_config(name: str) -> TaskConfig:
    """Load a task by name, searching all configured task directories.

    Raises :class:`TriggeredTaskError` if the name is found in more than one
    directory (ambiguous) or in none.
    """
    matches: list[Path] = []
    for d in all_task_dirs():
        candidate = d / f"{name}.md"
        if candidate.is_file():
            matches.append(candidate)
    if len(matches) > 1:
        locations = ", ".join(repo_relative(m) for m in matches)
        raise TriggeredTaskError(
            f"task '{name}' found in multiple directories: {locations}"
        )
    if not matches:
        raise TriggeredTaskError(f"task '{name}' not found in any task directory")
    return parse_frontmatter(matches[0])


def list_tasks() -> list[str]:
    """List all task names across all configured task directories.

    Raises :class:`TriggeredTaskError` if a name appears in more than one
    directory.
    """
    seen: dict[str, Path] = {}  # name → first directory it appeared in
    names: list[str] = []
    for d in all_task_dirs():
        if not d.is_dir():
            continue
        for path in d.glob("*.md"):
            if not path.is_file() or path.name in EXCLUDED_TASK_FILE_NAMES:
                continue
            name = path.stem
            if name in seen:
                raise TriggeredTaskError(
                    f"task '{name}' exists in both {repo_relative(seen[name])} "
                    f"and {repo_relative(d)}"
                )
            seen[name] = d
            names.append(name)
    return sorted(names)


# Import shared MCP config loader; keep the zero-arg wrapper for local use
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "Agents" / "lib"))
from mcp_config import load_mcp_servers as _load_mcp_servers_from_root


def _load_mcp_servers() -> dict:
    """Load MCP server definitions from .vscode/mcp.json."""
    return _load_mcp_servers_from_root(repo_root())


def resolve_mcp(name: str, agent: str = "claude") -> ResolvedMcp:
    servers = _load_mcp_servers()
    server = servers.get(name)
    if not isinstance(server, dict):
        return ResolvedMcp(flag=name)

    env = {str(key): str(value) for key, value in (server.get("env") or {}).items()}
    server_type = str(server.get("type") or "").strip().lower()
    if server_type == "http":
        url = str(server.get("url") or "").strip()
        if not url:
            return ResolvedMcp(flag=name, env=env)
        flag = f"remote --url {url}"
        oauth_client_id = str(server.get("oauthClientId") or "").strip()
        if oauth_client_id:
            flag = f"{flag} --entra-client-id {oauth_client_id}"
        return ResolvedMcp(flag=flag, env=env)

    command = server.get("command")
    if command != "npx":
        return ResolvedMcp(flag=name, env=env)

    args = [str(item) for item in server.get("args") or []]
    package_name = next((arg for arg in args if not arg.startswith("-")), None)
    if not package_name:
        return ResolvedMcp(flag=name, env=env)

    extras = [arg for arg in args if arg not in {"-y", package_name}]
    # Both agency and claude need --package; agency also needs --transport stdio
    # to prevent the npx proxy from defaulting to HTTP transport.
    flag = f"npx --package {package_name} --transport stdio"
    if extras:
        flag = f"{flag} -- {' '.join(extras)}"
    return ResolvedMcp(flag=flag, env=env)


def resolve_task_config(config: TaskConfig) -> TaskConfig:
    resolved_mcps: list[str] = []
    requested_mcps = list(config.requested_mcps) if config.requested_mcps is not None else list(config.mcps)
    env = dict(config.env)
    # Skip --mcp flags for servers the Copilot CLI will auto-load from
    # workspace config (.mcp.json) to avoid duplicate processes.
    ws_servers = set(workspace_mcp_server_names())
    for mcp_name in config.mcps:
        if config.agent in {"copilot", "agency copilot"} and mcp_name in ws_servers:
            continue
        resolved = resolve_mcp(mcp_name, agent=config.agent)
        resolved_mcps.append(resolved.flag)
        env.update(resolved.env)
    return replace(config, mcps=resolved_mcps, requested_mcps=requested_mcps, env=env)


def build_agent_flags(agent: str, mode: str, allow_tools: list[str] | None = None) -> list[str]:
    normalized_mode = "plan" if mode == "plan" else "write"
    resolved_allow_tools = list(allow_tools or [])
    if agent == "none":
        return []
    if agent in {"claude", "agency claude"}:
        return ["--permission-mode", "plan"] if normalized_mode == "plan" else ["--dangerously-skip-permissions"]
    if agent in {"copilot", "agency copilot"}:
        if normalized_mode != "plan":
            return ["--allow-all-tools", "--autopilot"]
        flags = ["--autopilot"]
        if "shell" not in resolved_allow_tools:
            flags.extend(["--deny-tool", "shell"])
        if "write" not in resolved_allow_tools:
            flags.extend(["--deny-tool", "write"])
        for tool_name in resolved_allow_tools:
            flags.extend(["--allow-tool", tool_name])
        return flags
    raise TriggeredTaskError(f"unknown agent '{agent}'")


def agent_binary(agent: str) -> list[str]:
    mapping = {
        "claude": ["claude"],
        "copilot": ["copilot"],
        "agency claude": ["agency", "claude"],
        "agency copilot": ["agency", "copilot"],
    }
    try:
        return mapping[agent]
    except KeyError as exc:
        raise TriggeredTaskError(f"unknown agent '{agent}'") from exc


@lru_cache(maxsize=8)
def agent_supported_flags(agent: str) -> frozenset[str]:
    try:
        completed = subprocess.run(
            [*agent_binary(agent), "--help"],
            capture_output=True,
            text=True,
            check=False,
            env={"HOME": os.environ.get("HOME", str(Path.home())), "PATH": clean_path()},
            timeout=AGENT_HELP_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    help_text = f"{completed.stdout}\n{completed.stderr}"
    supported_flags: set[str] = set()
    for match in re.finditer(r"(?<!\w)--[A-Za-z][A-Za-z0-9-]*", help_text):
        supported_flags.add(match.group(0))
    return frozenset(supported_flags)


def workspace_mcp_server_names() -> list[str]:
    """Return the names of all MCP servers the Copilot CLI will auto-load.

    Checks both .vscode/mcp.json (used by headless config) and .mcp.json
    (loaded by the Copilot CLI at runtime).
    """
    servers = set(_load_mcp_servers().keys())
    alt_path = repo_root() / ".mcp.json"
    if alt_path.is_file():
        try:
            import json as _json
            data = _json.loads(alt_path.read_text(encoding="utf-8"))
            alt = data.get("mcpServers") or data.get("servers") or {}
            servers.update(alt.keys())
        except (OSError, ValueError):
            pass
    return sorted(servers)


def build_agent_command(config: TaskConfig, prompt_text: str) -> list[str]:
    resolved = resolve_task_config(config)
    if resolved.agent == "none":
        raise TriggeredTaskError("agent: none does not have an agent command")

    command = [
        *agent_binary(resolved.agent),
        "-p",
        prompt_text,
        *build_agent_flags(resolved.agent, resolved.mode, resolved.allow_tools),
    ]
    if resolved.agent in {"claude", "agency claude"}:
        command.extend(["--output-format", "json", "--append-system-prompt", HEADLESS_PROMPT])
    else:
        command.extend(["--no-ask-user", "--no-custom-instructions"])
    if resolved.model:
        command.extend(["--model", resolved.model])
    for mcp in resolved.mcps:
        command.extend(["--mcp", mcp])
    if resolved.agent in {"copilot", "agency copilot"}:
        supported_flags = agent_supported_flags(resolved.agent)
        if "--no-default-mcps" in supported_flags:
            command.append("--no-default-mcps")
        # Disable workspace MCP servers the task doesn't need.  The copilot CLI
        # auto-loads all servers from .vscode/mcp.json; --disable-mcp-server
        # prevents specific ones from starting.  Without this, large MCP servers
        # (e.g. softeria-ms365 with 500+ tools) consume the entire context window.
        wanted = set(resolved.requested_mcps or [])
        if "--disable-mcp-server" in supported_flags:
            for server_name in workspace_mcp_server_names():
                if server_name not in wanted:
                    command.extend(["--disable-mcp-server", server_name])
        # Session transcript capture: --share writes a full markdown transcript
        # (tool calls, reasoning, results) to the specified path.
        if resolved.transcript and "--share" in supported_flags:
            ensure_logs_dir()
            command.extend(["--share", str(resolved.transcript_log)])
    return command


def resolve_task_command(name: str, prompt_text: str | None = None) -> str:
    config = load_task_config(name)
    if prompt_text is None:
        body = extract_prompt_body(config.prompt_path.read_text(encoding="utf-8"))
        prompt_text = body
    if config.agent == "none":
        if not config.handler_path:
            raise TriggeredTaskError("agent: none requires a handler")
        cmd = build_handler_command(config.handler_path)
        return shlex.join(cmd)
    return shlex.join(build_agent_command(config, prompt_text))


def build_agent_env(config: TaskConfig) -> dict[str, str]:
    env = {"HOME": os.environ.get("HOME", str(Path.home())), "PATH": clean_path()}
    env.update(resolve_task_config(config).env)
    return env


def _repair_json_quotes(text: str) -> str | None:
    """Attempt to fix unescaped double quotes and invalid escapes in JSON.

    LLMs sometimes produce JSON with:
    - Unescaped inner quotes (ASCII 0x22 inside string values)
    - Invalid escape sequences like \\> or \\* (markdown escapes in JSON strings)

    This function iteratively finds parse errors and fixes them.
    """
    repaired = text
    for _ in range(50):  # cap iterations
        try:
            json.loads(repaired)
            return repaired
        except json.JSONDecodeError as e:
            if e.pos is None or e.pos >= len(repaired):
                return None
            fixed = False
            # Case 1: Invalid escape sequence at error position
            if e.msg == "Invalid \\escape" and e.pos < len(repaired) - 1:
                # The backslash is at e.pos, followed by an invalid char
                # Fix by doubling the backslash (making it literal)
                repaired = repaired[:e.pos] + '\\' + repaired[e.pos:]
                fixed = True
            # Case 2: Unescaped quote - scan backwards from error position
            if not fixed:
                for pos in range(e.pos, max(-1, e.pos - 20), -1):
                    if pos >= len(repaired) or repaired[pos] != '"':
                        continue
                    num_backslashes = 0
                    p = pos - 1
                    while p >= 0 and repaired[p] == '\\':
                        num_backslashes += 1
                        p -= 1
                    if num_backslashes % 2 == 0:
                        repaired = repaired[:pos] + '\\"' + repaired[pos + 1:]
                        fixed = True
                        break
            if not fixed:
                return None
    return None


def extract_first_json_value(text: str) -> str:
    # Try all code fences; pick the largest one that parses as valid JSON.
    # The agent may output JSON via a shell tool (with unescaped inner quotes)
    # AND also echo valid JSON in its own response text.
    fenced_matches = list(re.finditer(r"```json\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE))
    best_fenced: str = ""
    largest_failed: str = ""
    for fenced_match in fenced_matches:
        candidate = fenced_match.group(1).strip()
        if not candidate:
            continue
        try:
            json.loads(candidate)
            if len(candidate) > len(best_fenced):
                best_fenced = candidate
        except json.JSONDecodeError:
            _diag = candidate[:200] if len(candidate) > 200 else candidate
            sys.stderr.write(f"[extract_json] code fence found ({len(candidate)} chars) but JSON parse failed; first 200: {repr(_diag)}\n")
            if len(candidate) > len(largest_failed):
                largest_failed = candidate
    if best_fenced:
        return best_fenced

    # If the largest code fence failed to parse, attempt repair
    if largest_failed and len(largest_failed) > len(best_fenced):
        repaired = _repair_json_quotes(largest_failed)
        if repaired:
            sys.stderr.write(f"[extract_json] repaired JSON from code fence ({len(largest_failed)} -> {len(repaired)} chars)\n")
            return repaired

    decoder = json.JSONDecoder()
    # Prefer dict matches over array matches (agent output is typically a dict
    # with results/why/actions keys; arrays inside tool responses are false positives).
    # Among dicts, prefer the largest one (agent output is bigger than tool responses).
    first_array = ""
    largest_dict = ""
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            _, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        extracted = text[index : index + end].strip()
        if char == "{":
            if len(extracted) > len(largest_dict):
                largest_dict = extracted
        elif not first_array:
            first_array = extracted
    if not fenced_matches and (largest_dict or first_array):
        chosen = largest_dict or first_array
        sys.stderr.write(f"[extract_json] no code fence match; using raw_decode fallback ({len(chosen)} chars, type={'dict' if largest_dict else 'array'})\n")
    return largest_dict or first_array


def filtered_copilot_output(text: str) -> str:
    cleaned_lines: list[str] = []
    for raw_line in ANSI_RE.sub("", text).replace("\r", "").splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith(COPILOT_NOISE_PREFIXES):
            continue
        cleaned_lines.append(line)
        if len(cleaned_lines) >= COPILOT_OUTPUT_MAX_LINES:
            break
    return "\n".join(cleaned_lines)


_USAGE_RE = re.compile(
    r"^\s*(\S+)\s+([\d.]+[kKmM]?)\s+in,\s+([\d.]+[kKmM]?)\s+out(?:,\s+([\d.]+[kKmM]?)\s+cached)?",
)
_PREMIUM_RE = re.compile(
    r"Total usage est:\s+([\d.]+)\s+Premium requests",
)
# Copilot / Agency (v2026.4.9+) compact format:
#   Requests  3 Premium (15s)
#   Tokens    ↑ 55.8k • ↓ 805 • 27.8k (cached)
_COPILOT_PREMIUM_RE = re.compile(
    r"Requests\s+([\d.]+)\s+Premium",
)
_COPILOT_TOKENS_RE = re.compile(
    r"Tokens\s+[↑⬆]\s*([\d.]+[kKmM]?)\s*[•·]\s*[↓⬇]\s*([\d.]+[kKmM]?)\s*[•·]\s*([\d.]+[kKmM]?)\s*\(cached\)",
)


def parse_usage_stats(stderr: str) -> dict[str, str | None]:
    """Extract model name, tokens, and premium requests from agent stderr.

    Supports multiple formats:
    - Old agency: ``Total usage est: N Premium requests`` / ``model Nk in, N out, N cached``
    - Copilot / new agency: ``Requests N Premium (Ns)`` / ``Tokens ↑ Nk • ↓ N • Nk (cached)``
    """
    result: dict[str, str | None] = {
        "model": None, "tokens_in": None, "tokens_out": None,
        "tokens_cached": None, "premium_requests": None,
    }
    for line in stderr.splitlines():
        # Old agency format
        m = _PREMIUM_RE.search(line)
        if m:
            result["premium_requests"] = m.group(1)
        # New copilot/agency format
        m = _COPILOT_PREMIUM_RE.search(line)
        if m:
            result["premium_requests"] = m.group(1)
        # Old agency format: ``model 162.3k in, 971 out, 0 cached``
        m = _USAGE_RE.search(line)
        if m:
            result["model"] = m.group(1)
            result["tokens_in"] = m.group(2)
            result["tokens_out"] = m.group(3)
            result["tokens_cached"] = m.group(4)
        # New copilot/agency format: ``Tokens ↑ 55.8k • ↓ 805 • 27.8k (cached)``
        m = _COPILOT_TOKENS_RE.search(line)
        if m:
            result["tokens_in"] = m.group(1)
            result["tokens_out"] = m.group(2)
            result["tokens_cached"] = m.group(3)
    return result


def parse_claude_json_output(raw_output: str) -> tuple[str, dict[str, str | None]]:
    """Parse claude ``--output-format json`` response.

    Returns ``(result_text, usage_dict)`` where *usage_dict* has the same keys
    as ``parse_usage_stats()``.  Falls through gracefully if the JSON is
    malformed — returns the raw output string and empty usage.
    """
    usage: dict[str, str | None] = {
        "model": None, "tokens_in": None, "tokens_out": None,
        "tokens_cached": None, "premium_requests": None,
    }
    try:
        data = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError):
        return raw_output, usage

    result_text = data.get("result", raw_output)

    # Extract usage from claude JSON response
    u = data.get("usage", {})
    input_tokens = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    output_tokens = u.get("output_tokens", 0)
    cached_tokens = u.get("cache_read_input_tokens", 0)

    def _fmt(n: int) -> str:
        if n >= 1000:
            return f"{n / 1000:.1f}k"
        return str(n)

    usage["tokens_in"] = _fmt(input_tokens)
    usage["tokens_out"] = _fmt(output_tokens)
    usage["tokens_cached"] = _fmt(cached_tokens)

    # Extract cost
    cost = data.get("total_cost_usd")
    if cost is not None:
        usage["cost_usd"] = f"{cost:.4f}"

    # Extract model from modelUsage keys
    model_usage = data.get("modelUsage", {})
    if model_usage:
        usage["model"] = next(iter(model_usage))

    return result_text, usage


def normalize_agent_output(config: TaskConfig, raw_output: str) -> tuple[str, dict | list | None]:
    """Normalize agent stdout, returning (text, parsed_json_or_None)."""
    extracted = extract_first_json_value(raw_output)
    if extracted:
        try:
            parsed = json.loads(extracted)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        return extracted.strip(), parsed

    raw_stripped = raw_output.strip()
    if config.agent == "copilot":
        stripped = filtered_copilot_output(raw_output).strip()
    else:
        stripped = raw_stripped

    looks_like_json_payload = (
        raw_stripped.startswith(("{", "["))
        or re.match(r"```json\b", raw_stripped, flags=re.IGNORECASE) is not None
    )
    if raw_stripped and (config.post_processor or config.handler or looks_like_json_payload):
        log_event(
            config.task_log,
            phase="agent",
            level="error",
            message="failed to parse JSON from agent output; raw output follows",
            error_category="output_parse_error",
        )
        log_event(
            config.task_log,
            phase="agent",
            level="error",
            message=f"[raw-output] {raw_stripped[:MAX_RAW_OUTPUT_LOG_LENGTH]}",
        )
    return stripped, None


def _persist_run_output(config: TaskConfig, stdout: str, stderr: str, *, label: str = "") -> str | None:
    """Persist full stdout/stderr (and transcript if present) from an agent run.

    Files are named: <task>-<YYYYMMDDTHHMMSSZ>[-label].{stdout.txt,stderr.txt,transcript.md}
    The first '.' separates the identity (task + timestamp + label) from the type.
    """
    runs_dir = logs_root() / "runs"
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        ts_slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = f"-{label}" if label else ""
        base = f"{config.name}-{ts_slug}{suffix}"
        (runs_dir / f"{base}.stdout.txt").write_text(stdout or "(empty)", encoding="utf-8")
        (runs_dir / f"{base}.stderr.txt").write_text(stderr or "(empty)", encoding="utf-8")
        # Archive the session transcript before the next run overwrites it
        t_log = config.transcript_log
        if t_log.is_file() and t_log.stat().st_size > 0:
            shutil.copy2(t_log, runs_dir / f"{base}.transcript.md")
        return str(runs_dir / base)
    except OSError:
        return None


def headless_agent(config: TaskConfig, prompt_text: str, *, stream: bool = False) -> AgentResult:
    resolved = resolve_task_config(config)
    if resolved.agent == "none":
        raise TriggeredTaskError("agent: none cannot be executed through headless_agent")

    env = build_agent_env(resolved)
    command = build_agent_command(resolved, prompt_text)
    timeout = config.timeout or HEADLESS_TIMEOUT

    empty_output_attempts = HEADLESS_EMPTY_OUTPUT_RETRIES + 1
    attempts = empty_output_attempts + HEADLESS_TIMEOUT_RETRIES
    timeout_count = 0
    empty_count = 0
    for attempt in range(1, attempts + 1):
        try:
            if resolved.agent == "copilot":
                raw_output, stderr_text = run_copilot_with_pty(command, env, stream=stream, timeout=timeout)
            elif stream:
                raw_output, stderr_text = _run_agent_streaming(command, env, config, timeout=timeout)
            else:
                completed = subprocess.run(
                    command,
                    cwd=repo_root(),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                stderr_text = completed.stderr
                if completed.returncode != 0:
                    if stderr_text:
                        log_event(config.task_log, phase="agent", level="error",
                                  message=stderr_text[:MAX_LOG_FIELD_LENGTH],
                                  error_category="cli_crash",
                                  traceback=_extract_traceback(stderr_text))
                    if completed.stdout:
                        log_event(config.task_log, phase="agent", level="error",
                                  message=f"[stdout] {completed.stdout.strip()[:MAX_LOG_FIELD_LENGTH]}")
                    detail = stderr_text.strip()[:MAX_LOG_FIELD_LENGTH] if stderr_text else ""
                    raise TriggeredTaskError(
                        f"agent exited with status {completed.returncode}: {detail or ' '.join(command)}"
                    )
                raw_output = completed.stdout
        except subprocess.TimeoutExpired as exc:
            timeout_count += 1
            # Capture any partial output from the timed-out process
            partial_stdout = ""
            partial_stderr = ""
            if hasattr(exc, "stdout") and exc.stdout:
                partial_stdout = exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode("utf-8", errors="replace")
            if hasattr(exc, "stderr") and exc.stderr:
                partial_stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode("utf-8", errors="replace")

            # Persist output for post-mortem analysis
            _persist_run_output(config, partial_stdout, partial_stderr, label="timeout")

            if timeout_count <= HEADLESS_TIMEOUT_RETRIES:
                # Log warning and retry
                log_event(config.task_log, phase="agent", level="warning",
                          message=f"agent timed out after {timeout}s (timeout attempt {timeout_count}/{HEADLESS_TIMEOUT_RETRIES + 1}); retrying",
                          error_category="timeout", timeout_s=timeout, attempt=timeout_count)
                continue

            # Final timeout attempt - log and raise
            msg = (f"agent timed out after {timeout}s on retry (attempt {timeout_count}/{HEADLESS_TIMEOUT_RETRIES + 1}); giving up"
                   if HEADLESS_TIMEOUT_RETRIES > 0
                   else f"agent timed out after {timeout}s")
            log_event(config.task_log, phase="agent", level="error",
                      message=msg,
                      error_category="timeout", timeout_s=timeout, attempt=timeout_count)
            raise TriggeredTaskError(msg) from exc
        except TriggeredTaskError as exc:
            # Catch timeouts raised by run_copilot_with_pty / _run_agent_streaming
            if "timed out" in str(exc):
                timeout_count += 1
                _persist_run_output(config, "", "", label="timeout")

                if timeout_count <= HEADLESS_TIMEOUT_RETRIES:
                    log_event(config.task_log, phase="agent", level="warning",
                              message=f"agent timed out after {timeout}s (timeout attempt {timeout_count}/{HEADLESS_TIMEOUT_RETRIES + 1}); retrying",
                              error_category="timeout", timeout_s=timeout, attempt=timeout_count)
                    continue

                msg = (f"agent timed out after {timeout}s on retry (attempt {timeout_count}/{HEADLESS_TIMEOUT_RETRIES + 1}); giving up"
                       if HEADLESS_TIMEOUT_RETRIES > 0
                       else f"agent timed out after {timeout}s")
                log_event(config.task_log, phase="agent", level="error",
                          message=msg,
                          error_category="timeout", timeout_s=timeout, attempt=timeout_count)
                raise TriggeredTaskError(msg) from exc
            raise
        except FileNotFoundError as exc:
            log_event(config.task_log, phase="agent", level="error",
                      message=f"required command not found: {exc.filename}",
                      error_category="cli_crash")
            raise TriggeredTaskError(f"required command not found: {exc.filename}") from exc

        if stderr_text:
            log_event(config.task_log, phase="agent", level="info", message=stderr_text[:MAX_LOG_FIELD_LENGTH])

        # Claude with --output-format json: unwrap the JSON envelope and
        # extract usage stats from the structured response.
        claude_usage: dict[str, str | None] | None = None
        if resolved.agent in {"claude", "agency claude"}:
            raw_output, claude_usage = parse_claude_json_output(raw_output)

        stripped, parsed = normalize_agent_output(resolved, raw_output)
        if config.post_processor and stripped and parsed is None:
            raise TriggeredTaskError(
                "failed to parse JSON from agent output for "
                f"post-processor {config.post_processor_reference}; refusing to invoke handler"
            )

        if stripped:
            if claude_usage and claude_usage.get("tokens_in"):
                usage = claude_usage
            else:
                usage = parse_usage_stats(stderr_text)
            # Determine transcript path if transcript capture was enabled
            t_path: str | None = None
            if resolved.transcript and resolved.agent in {"copilot", "agency copilot"}:
                t_log = resolved.transcript_log
                if t_log.is_file() and t_log.stat().st_size > 0:
                    t_path = str(t_log)
            # Always persist full run output
            _persist_run_output(config, raw_output, stderr_text)
            return AgentResult(
                output=stripped,
                stderr=stderr_text,
                model=usage.get("model"),
                tokens_in=usage.get("tokens_in"),
                tokens_out=usage.get("tokens_out"),
                tokens_cached=usage.get("tokens_cached"),
                premium_requests=usage.get("premium_requests"),
                cost_usd=usage.get("cost_usd"),
                transcript_path=t_path,
                structured_output=parsed,
            )

        # --- Empty output diagnostics and transcript fallback ---
        # Determine why output is empty: truly no stdout, or content that
        # normalized to empty (e.g. only copilot noise lines)?
        empty_count += 1
        raw_was_nonempty = bool(raw_output.strip())
        t_log = resolved.transcript_log
        t_size = t_log.stat().st_size if t_log.is_file() else 0
        usage_info = parse_usage_stats(stderr_text)

        # Classify the empty-output cause for structured logging
        if not raw_output.strip():
            empty_cause = "stdout_empty"
        else:
            empty_cause = "normalize_stripped"

        # Attempt transcript fallback: the agent may have produced output in
        # its transcript but not stdout (known agency copilot handoff issue).
        transcript_fallback: str | None = None
        if resolved.transcript and resolved.agent in {"copilot", "agency copilot"} and t_size > 0:
            try:
                t_content = t_log.read_text(encoding="utf-8", errors="replace")
                t_extracted = extract_first_json_value(t_content)
                if t_extracted:
                    transcript_fallback = t_extracted
            except OSError:
                pass

        if transcript_fallback:
            # Successfully recovered output from transcript
            log_event(
                config.task_log,
                phase="agent",
                level="warning",
                message=(
                    f"stdout was empty on attempt {empty_count}/{empty_output_attempts} but "
                    f"recovered output from transcript ({len(transcript_fallback)} chars)"
                ),
                empty_cause=empty_cause,
                recovery="transcript_fallback",
                raw_stdout_len=len(raw_output),
                transcript_size=t_size,
                premium_requests=usage_info.get("premium_requests"),
                tokens_out=usage_info.get("tokens_out"),
            )
            fallback_stripped, fallback_parsed = normalize_agent_output(resolved, transcript_fallback)
            if fallback_stripped:
                if config.post_processor and fallback_parsed is None:
                    raise TriggeredTaskError(
                        "failed to parse JSON from transcript-recovered output for "
                        f"post-processor {config.post_processor_reference}; refusing to invoke handler"
                    )
                # Always persist full run output
                _persist_run_output(config, raw_output, stderr_text, label="transcript-fallback")
                return AgentResult(
                    output=fallback_stripped,
                    stderr=stderr_text,
                    model=usage_info.get("model"),
                    tokens_in=usage_info.get("tokens_in"),
                    tokens_out=usage_info.get("tokens_out"),
                    tokens_cached=usage_info.get("tokens_cached"),
                    premium_requests=usage_info.get("premium_requests"),
                    cost_usd=usage_info.get("cost_usd"),
                    transcript_path=str(t_log),
                    structured_output=fallback_parsed,
                )

        # Persist raw stdout/stderr for post-mortem analysis
        debug_file = _persist_run_output(config, raw_output, stderr_text, label="empty")

        # Construct a short stdout preview for the log (first meaningful chars)
        stdout_preview = ""
        if raw_was_nonempty:
            preview_text = raw_output.strip()[:200].replace("\n", "\\n")
            stdout_preview = preview_text

        # Emit structured diagnostic log entry
        if empty_count < empty_output_attempts:
            log_event(
                config.task_log,
                phase="agent",
                level="warning",
                message=(
                    f"agent returned empty output on attempt {empty_count}/{empty_output_attempts}; "
                    f"retrying in {HEADLESS_EMPTY_OUTPUT_RETRY_DELAY_S:g}s"
                ),
                empty_cause=empty_cause,
                agent=resolved.agent,
                raw_stdout_len=len(raw_output),
                stdout_preview=stdout_preview or None,
                transcript_size=t_size,
                transcript_path=str(t_log) if t_size > 0 else None,
                transcript_fallback_attempted=bool(resolved.transcript and t_size > 0),
                premium_requests=usage_info.get("premium_requests"),
                tokens_in=usage_info.get("tokens_in"),
                tokens_out=usage_info.get("tokens_out"),
                tokens_cached=usage_info.get("tokens_cached"),
                debug_file=debug_file,
            )
            time.sleep(HEADLESS_EMPTY_OUTPUT_RETRY_DELAY_S)
        else:
            log_event(
                config.task_log,
                phase="agent",
                level="error",
                message=(
                    f"agent returned empty output on all {empty_output_attempts} attempts"
                ),
                error_category="empty_output",
                empty_cause=empty_cause,
                agent=resolved.agent,
                raw_stdout_len=len(raw_output),
                stdout_preview=stdout_preview or None,
                transcript_size=t_size,
                transcript_path=str(t_log) if t_size > 0 else None,
                transcript_fallback_attempted=bool(resolved.transcript and t_size > 0),
                premium_requests=usage_info.get("premium_requests"),
                tokens_in=usage_info.get("tokens_in"),
                tokens_out=usage_info.get("tokens_out"),
                tokens_cached=usage_info.get("tokens_cached"),
                debug_file=debug_file,
            )

    raise TriggeredTaskError(
        f"agent returned empty output after {empty_output_attempts} attempts; "
        f"debug files in {logs_root() / 'runs'}"
    )


def _run_agent_streaming(command: list[str], env: dict[str, str], config: TaskConfig, *, timeout: int = HEADLESS_TIMEOUT) -> tuple[str, str]:
    """Run an agent subprocess while tee-ing stdout to the terminal in real time."""
    proc = subprocess.Popen(
        command,
        cwd=repo_root(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout_lines: list[str] = []
    try:
        assert proc.stdout is not None  # noqa: S101
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            stdout_lines.append(line)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise TriggeredTaskError(f"agent timed out after {timeout}s") from exc

    stderr_text = proc.stderr.read() if proc.stderr else ""
    if proc.returncode != 0:
        stdout_text = "".join(stdout_lines)
        if stderr_text:
            log_event(config.task_log, phase="agent", level="error",
                      message=stderr_text[:MAX_LOG_FIELD_LENGTH],
                      error_category="cli_crash",
                      traceback=_extract_traceback(stderr_text))
        if stdout_text:
            log_event(config.task_log, phase="agent", level="error",
                      message=f"[stdout] {stdout_text.strip()[:MAX_LOG_FIELD_LENGTH]}")
        detail = stderr_text.strip()[:MAX_LOG_FIELD_LENGTH] if stderr_text else ""
        raise TriggeredTaskError(
            f"agent exited with status {proc.returncode}: {detail or ' '.join(command)}"
        )
    return "".join(stdout_lines), stderr_text


def run_copilot_with_pty(command: list[str], env: dict[str, str], *, stream: bool = False, timeout: int = HEADLESS_TIMEOUT) -> tuple[str, str]:
    env_items = ["env", "-i", *(f"{key}={value}" for key, value in env.items())]
    command_string = shlex.join(env_items + command)
    with tempfile.NamedTemporaryFile(prefix="triggered-task-copilot-", delete=False) as handle:
        transcript_path = Path(handle.name)

    try:
        if stream:
            # Use 'script' with stdout flowing to the terminal in real time
            completed = subprocess.run(
                ["script", "-qc", command_string, str(transcript_path)],
                cwd=repo_root(),
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        else:
            completed = subprocess.run(
                ["script", "-qc", command_string, str(transcript_path)],
                cwd=repo_root(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        if completed.returncode != 0:
            transcript = ""
            try:
                transcript = transcript_path.read_text(encoding="utf-8", errors="ignore").strip()[:MAX_LOG_FIELD_LENGTH]
            except OSError:
                pass
            detail = completed.stderr.strip()[:MAX_LOG_FIELD_LENGTH] or transcript or " ".join(command)
            raise TriggeredTaskError(f"agent exited with status {completed.returncode}: {detail}")
        cleaned = ANSI_RE.sub("", transcript_path.read_text(encoding="utf-8", errors="ignore")).replace("\r", "")
        return cleaned, completed.stderr
    finally:
        transcript_path.unlink(missing_ok=True)


# Base packages injected into every Python handler run.
# Add here when a package is used by multiple handlers.
BASE_HANDLER_PACKAGES = ["mcp[cli]"]


def _find_uv() -> str | None:
    """Locate the ``uv`` binary, searching common install paths if needed.

    ``shutil.which`` relies on the current process PATH which is minimal under
    cron or file-watcher contexts.  Fall back to well-known locations so that
    Python handlers can always be executed via ``uv run --with …``.
    """
    found = shutil.which("uv")
    if found:
        return found
    # Also search the directories included in clean_path() / common installs
    candidates = [
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".cargo" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def build_handler_command(handler_path: Path) -> list[str]:
    """Build the full command list to execute a handler by file extension."""
    suffix = handler_path.suffix.lower()
    if suffix == ".py":
        uv = _find_uv()
        if uv:
            cmd = [uv, "run"]
            for pkg in BASE_HANDLER_PACKAGES:
                cmd += ["--with", pkg]
            cmd.append(str(handler_path))
            return cmd
        return [sys.executable, str(handler_path)]
    if suffix in (".js", ".ts"):
        return ["node", str(handler_path)]
    return ["bash", str(handler_path)]


def run_handler(config: TaskConfig, input_text: str | None, *, changed_files: list[str] | None = None) -> str:
    if not config.handler_path:
        raise TriggeredTaskError("handler is required")
    if not config.handler_path.is_file():
        raise TriggeredTaskError(f"handler not found: {config.handler_reference}")

    env = os.environ.copy()
    env.update(resolve_task_config(config).env)
    if changed_files:
        env["TRIGGERED_CHANGED_FILES"] = json.dumps(changed_files)
    run_kwargs: dict[str, Any] = {
        "cwd": repo_root(),
        "capture_output": True,
        "text": True,
        "env": env,
        "check": False,
    }
    # Match the old shell behavior: handler-only runs should see closed stdin.
    if input_text is None:
        run_kwargs["stdin"] = subprocess.DEVNULL
    else:
        run_kwargs["input"] = input_text
    cmd = build_handler_command(config.handler_path)
    completed = subprocess.run(cmd, **run_kwargs)
    if completed.returncode != 0:
        stderr_text = completed.stderr.strip() if completed.stderr else ""
        if stderr_text:
            log_event(config.task_log, phase="handler", level="error", message=stderr_text,
                      error_category="handler_crash",
                      traceback=_extract_traceback(stderr_text))
        detail = stderr_text[:MAX_LOG_FIELD_LENGTH] if stderr_text else config.handler_reference
        raise TriggeredTaskError(
            f"handler failed with status {completed.returncode}: {detail}"
        )
    if completed.stderr:
        log_event(config.task_log, phase="handler", level="info", message=completed.stderr.rstrip("\n"))
    return completed.stdout.rstrip("\n")


def run_post_processor(config: TaskConfig, input_text: str | None, *, changed_files: list[str] | None = None) -> str:
    """Alias for run_handler — runs the post-processor (or handler) script."""
    return run_handler(config, input_text, changed_files=changed_files)


def run_pre_processor(config: TaskConfig, *, changed_files: list[str] | None = None) -> PreProcessorResult:
    """Run the pre-processor script and return its output with skip detection."""
    if not config.pre_processor_path:
        raise TriggeredTaskError("pre-processor is required")
    if not config.pre_processor_path.is_file():
        raise TriggeredTaskError(f"pre-processor not found: {config.pre_processor_reference}")

    env = os.environ.copy()
    env.update(resolve_task_config(config).env)
    if changed_files:
        env["TRIGGERED_CHANGED_FILES"] = json.dumps(changed_files)

    cmd = build_handler_command(config.pre_processor_path)
    completed = subprocess.run(
        cmd, cwd=repo_root(), capture_output=True, text=True,
        env=env, check=False, stdin=subprocess.DEVNULL,
    )
    stderr_text = completed.stderr.strip() if completed.stderr else ""
    if completed.returncode != 0:
        if stderr_text:
            log_event(config.task_log, phase="pre-processor", level="error", message=stderr_text,
                      error_category="pre_processor_crash",
                      traceback=_extract_traceback(stderr_text))
        detail = stderr_text[:MAX_LOG_FIELD_LENGTH] if stderr_text else config.pre_processor_reference
        raise TriggeredTaskError(
            f"pre-processor failed with status {completed.returncode}: {detail}"
        )
    output = completed.stdout.rstrip("\n")
    skip = False
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict) and parsed.get("skip"):
            skip = True
    except (json.JSONDecodeError, ValueError):
        pass
    if stderr_text:
        log_event(config.task_log, phase="pre-processor", level="info", message=stderr_text)
    return PreProcessorResult(output=output, skip=skip, stderr=stderr_text)


def current_crontab_lines() -> list[str] | None:
    """Return crontab lines, or None if crontab is unavailable (e.g. sandbox)."""
    try:
        completed = subprocess.run(
            ["crontab", "-l"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise TriggeredTaskError("crontab command not found") from exc
    if completed.returncode != 0:
        return None
    return [line for line in completed.stdout.splitlines() if line.strip()]


def install_crontab(lines: list[str]) -> None:
    payload = "\n".join(lines) + "\n" if lines else ""
    try:
        subprocess.run(
            ["crontab", "-"],
            cwd=repo_root(),
            input=payload,
            text=True,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise TriggeredTaskError("crontab command not found") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else "failed to update crontab"
        raise TriggeredTaskError(stderr) from exc


def remove_cron_entries(name: str) -> bool:
    lines = current_crontab_lines()
    if lines is None:
        raise TriggeredTaskError("crontab is not accessible")
    filtered = [line for line in lines if name not in line]
    if len(filtered) == len(lines):
        return False
    install_crontab(filtered)
    return True


def cron_is_active(name: str) -> bool | None:
    """Return True if active, False if inactive, None if crontab unavailable."""
    lines = current_crontab_lines()
    if lines is None:
        return None
    return any(name in line for line in lines)


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _find_all_watcher_pids(name: str) -> list[int]:
    proc_dir = Path("/proc")
    if proc_dir.is_dir():
        return _find_watcher_pids_proc(name, proc_dir)
    return _find_watcher_pids_ps(name)


def _find_watcher_pids_proc(name: str, proc_dir: Path) -> list[int]:
    """Read /proc to find watcher processes (avoids subprocess)."""
    pids: list[int] = []
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        cmdline = raw.decode("utf-8", errors="replace").replace("\x00", " ")
        if "activate.py" not in cmdline:
            continue
        if f"--watch-loop {name}" not in cmdline:
            continue
        pids.append(int(entry.name))
    return pids


def _find_watcher_pids_ps(name: str) -> list[int]:
    """Fallback for non-Linux systems using ps."""
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    pids: list[int] = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped or "activate.py" not in stripped:
            continue
        if f"--watch-loop {name}" not in stripped:
            continue
        pid_text, _, _ = stripped.partition(" ")
        try:
            pids.append(int(pid_text))
        except ValueError:
            continue
    return pids


def find_watcher_pid(name: str) -> int | None:
    pids = _find_all_watcher_pids(name)
    return pids[0] if pids else None


def _wait_for_pids_to_stop(
    pids: list[int],
    stopped_pids: set[int],
    timeout_s: float,
) -> int | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for pid in pids:
            if pid in stopped_pids:
                continue
            if not pid_is_running(pid):
                stopped_pids.add(pid)
        if len(stopped_pids) == len(pids):
            return next(iter(stopped_pids))
        time.sleep(0.1)
    return None


def stop_watcher(name: str) -> int | None:
    """Stop all live watcher processes for a task and return the first stopped PID found."""
    pids = [pid for pid in _find_all_watcher_pids(name) if pid_is_running(pid)]
    if not pids:
        return None

    stopped_pids: set[int] = set()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue

    stopped_pid = _wait_for_pids_to_stop(pids, stopped_pids, timeout_s=5)
    if stopped_pid is not None:
        return stopped_pid

    for pid in pids:
        if pid in stopped_pids:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            # A missing PID is already fully stopped, so treat it as complete here.
            stopped_pids.add(pid)

    stopped_pid = _wait_for_pids_to_stop(pids, stopped_pids, timeout_s=2)
    if stopped_pid is not None:
        return stopped_pid

    return next(iter(stopped_pids)) if stopped_pids else None



def _trigger_states(config: TaskConfig) -> dict[str, str]:
    """Return per-trigger state for multi-trigger tasks."""
    states: dict[str, str] = {}
    if config.schedule:
        active = cron_is_active(config.name)
        if active is None:
            states["cron"] = "unknown"
        elif active:
            states["cron"] = "active"
        else:
            states["cron"] = "stopped"
    if config.watch_path:
        pid = find_watcher_pid(config.name)
        if pid is not None:
            states["watcher"] = f"active (pid {pid})"
        else:
            states["watcher"] = "stopped"
    return states


def task_state(config: TaskConfig) -> str:
    try:
        task_type = config.task_type
    except TriggeredTaskError:
        return "stopped"
    if task_type == "multi":
        states = _trigger_states(config)
        if all(s == "unknown" for s in states.values()):
            return "unknown"
        if all(s.startswith("active") or s == "unknown" for s in states.values()):
            return "active"
        if any(s.startswith("active") for s in states.values()):
            return "partial"
        return "stopped"
    if task_type == "cron":
        active = cron_is_active(config.name)
        if active is None:
            return "unknown"
        if active:
            return "active"
        return "stopped"
    pid = find_watcher_pid(config.name)
    if pid is not None:
        return f"active (pid {pid})"
    return "stopped"


def task_details(config: TaskConfig) -> dict[str, Any]:
    details: dict[str, Any] = {
        "name": config.name,
        "type": config.task_type,
        "agent": config.agent,
        "mode": config.mode,
        "promptPath": config.prompt_reference,
        "state": task_state(config),
    }
    if config.model:
        details["model"] = config.model
    if config.pre_processor:
        details["pre-processor"] = config.pre_processor_reference
    if config.handler_reference:
        details["post-processor"] = config.handler_reference
    if config.schedule:
        details["schedule"] = config.schedule
    if config.watch_path:
        details["watchPath"] = config.watch_path
    if config.mcps:
        details["mcps"] = config.mcps
    if config.transcript:
        details["transcript"] = True
    try:
        details["triggerStates"] = _trigger_states(config)
    except TriggeredTaskError:
        pass
    return details
