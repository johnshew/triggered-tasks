#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp", "msal", "httpx"]
# ///
"""Pre-processor for smoketest-mcp-work.

Tests the handler path: calls ask_work_iq directly via stdio MCP.
Reports pass/fail. Skips the agent when auth fails to avoid wasting
2x60s on a guaranteed timeout.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path.cwd()

sys.path.insert(0, str(ROOT / "Agents" / "lib"))
from mcp_config import build_stdio_params, get_server_config

PREFIX = "[smoketest-mcp-work-prep]"


def log(msg: str) -> None:
    print(f"{PREFIX} {msg}", file=sys.stderr)


async def call_ask_work_iq() -> tuple[bool, str]:
    """Connect to workiq-cli and call ask_work_iq."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    for name in ("mcp.client.stdio", "mcp"):
        logging.getLogger(name).setLevel(logging.ERROR)

    server_config = get_server_config(ROOT, "workiq-cli")
    params = build_stdio_params(server_config)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=30)
            result = await asyncio.wait_for(
                session.call_tool("ask_work_iq", {"question": "What is today's date?"}),
                timeout=60,
            )
            text = ""
            for item in result.content:
                if hasattr(item, "text"):
                    text += item.text
            if not text:
                return False, "empty response"
            # Check if response contains an auth or API error
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and parsed.get("error"):
                    return False, f"API error: {str(parsed['error'])[:150]}"
                if isinstance(parsed, dict) and parsed.get("response") is None:
                    return False, "null response from ask_work_iq"
            except (json.JSONDecodeError, TypeError):
                pass  # non-JSON text is fine, treat as success
            return True, text[:200]


def main() -> None:
    handler_passed = False
    try:
        handler_passed, detail = asyncio.run(call_ask_work_iq())
    except Exception as exc:
        log(f"FAIL (handler path): {exc}")
        detail = str(exc)[:120]

    if handler_passed:
        log(f"OK (handler path): ask_work_iq responded: {detail[:80]}")
    else:
        log(f"FAIL (handler path): {detail}")

    # Skip the agent if the handler path failed -- auth issues will cause
    # the agent to timeout too, wasting 2x60s for no diagnostic value.
    output = {
        "skip": not handler_passed,
        "handler_result": "PASS" if handler_passed else "FAIL",
        "handler_detail": detail[:200] if not handler_passed else "",
    }
    print(json.dumps(output))

if __name__ == "__main__":
    main()
