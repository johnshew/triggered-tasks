#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Pre-release audit: scan for personal information, secrets, and portability issues.

Run from repo root:
    uv run Agents/scripts/pre-release-audit.py [--fix-paths]

Exit code 0 = pass, 1 = findings reported.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Directories included in a release (relative to repo root)
INCLUDED_DIRS = [
    ".claude/skills/triggered-task",
    ".agents",
    "Agents",
]
INCLUDED_FILES = ["AGENTS.md"]

# Directories/files that are always excluded even inside included dirs
EXCLUDED_PATTERNS = {
    "Agents/logs",
    "Agents/data",
    ".git",
    "__pycache__",
    "node_modules",
}

# File extensions to scan
TEXT_EXTENSIONS = {
    ".md", ".py", ".sh", ".yaml", ".yml", ".json", ".toml",
    ".txt", ".cfg", ".ini", ".env",
}

# Patterns that indicate potential secrets
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("API key",        re.compile(r"(?i)(api[_-]?key|apikey)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}")),
    ("Bearer token",   re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-.]{20,}")),
    ("Private key",    re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----")),
    ("AWS key",        re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token",   re.compile(r"gh[ps]_[A-Za-z0-9_]{36,}")),
    ("Generic secret", re.compile(r"(?i)(secret|password|passwd|token)\s*[:=]\s*['\"][^'\"]{8,}['\"]")),
]

# Patterns that indicate hardcoded personal paths
PATH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Home directory",  re.compile(r"/home/[a-z][a-z0-9_-]+/(?!runner/)")),
    ("Windows user",    re.compile(r"C:\\Users\\[A-Za-z][A-Za-z0-9_-]+\\")),
    ("macOS user",      re.compile(r"/Users/[A-Za-z][A-Za-z0-9_-]+/")),
]

# Patterns that indicate personal information (names, emails)
PERSONAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Email address", re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")),
]

# Known-safe patterns to suppress false positives
SAFE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"example\.com"),
    re.compile(r"user@"),
    re.compile(r"someone@"),
    re.compile(r"noreply@"),
    re.compile(r"\.token-cache\.json"),  # Path pattern, not a token
    re.compile(r"WORKIQ_MAIL_TOKEN_CACHE_PATH"),  # Env var name, not value
    re.compile(r"MS365_MCP_TOKEN_CACHE_PATH"),
    re.compile(r"/home/runner/"),  # CI runner paths are fine
    re.compile(r"~/.config/"),  # Standard XDG paths with tilde are portable
    re.compile(r"Path\.home\(\)"),  # Python dynamic home resolution
    re.compile(r"/home/user/"),  # Generic doc example paths
    re.compile(r"/home/you/"),  # Generic doc example paths
    re.compile(r"alice@|bob@|jane@"),  # Common doc example names
]


def repo_root() -> Path:
    """Walk up from this script to find the repo root (contains .git)."""
    candidate = Path(__file__).resolve().parent
    while candidate != candidate.parent:
        if (candidate / ".git").is_dir():
            return candidate
        candidate = candidate.parent
    return Path.cwd()


def should_scan(path: Path, root: Path) -> bool:
    """Return True if the file should be scanned."""
    relative = path.relative_to(root)
    rel_str = str(relative)

    # Skip excluded directories
    for excluded in EXCLUDED_PATTERNS:
        if rel_str.startswith(excluded) or f"/{excluded}/" in f"/{rel_str}/":
            return False

    # Skip hidden files/dirs (except .claude, .agents)
    parts = relative.parts
    for part in parts[:-1]:  # Check parent dirs
        if part.startswith(".") and part not in {".claude", ".agents"}:
            return False

    # Only scan text files
    return path.suffix.lower() in TEXT_EXTENSIONS


def collect_files(root: Path) -> list[Path]:
    """Collect all files to scan."""
    files: list[Path] = []

    for dir_path in INCLUDED_DIRS:
        full = root / dir_path
        if full.is_dir():
            for path in sorted(full.rglob("*")):
                if path.is_file() and should_scan(path, root):
                    files.append(path)

    for file_name in INCLUDED_FILES:
        full = root / file_name
        if full.is_file():
            files.append(full)

    return files


def is_safe_match(line: str) -> bool:
    """Return True if the match is a known false positive."""
    return any(pat.search(line) for pat in SAFE_PATTERNS)


def scan_file(path: Path, root: Path) -> list[str]:
    """Scan a single file and return a list of findings."""
    findings: list[str] = []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings

    relative = path.relative_to(root)
    lines = content.splitlines()

    for line_num, line in enumerate(lines, 1):
        if is_safe_match(line):
            continue

        for label, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(f"  {relative}:{line_num}: {label}")

        for label, pattern in PATH_PATTERNS:
            if pattern.search(line):
                findings.append(f"  {relative}:{line_num}: Hardcoded {label}")

        for label, pattern in PERSONAL_PATTERNS:
            if pattern.search(line):
                findings.append(f"  {relative}:{line_num}: {label}")

    return findings


def main() -> int:
    root = repo_root()
    files = collect_files(root)

    if not files:
        print("No files found to scan.", file=sys.stderr)
        return 1

    all_findings: list[str] = []
    for path in files:
        all_findings.extend(scan_file(path, root))

    print(f"Pre-release audit: scanned {len(files)} files")
    print()

    if all_findings:
        print(f"⚠️  {len(all_findings)} finding(s):")
        print()
        for finding in all_findings:
            print(finding)
        print()
        print("Review each finding. If it is a false positive, add a safe")
        print("pattern to SAFE_PATTERNS in this script.")
        return 1

    print("✅ No issues found. Ready for release.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
