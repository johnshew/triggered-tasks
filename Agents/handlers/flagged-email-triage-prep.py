#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["msal", "httpx", "mcp", "html2text"]
# ///
"""Pre-processor for flagged-email-triage.

Pre-fetches email thread content via MCP before the agent runs, so the
agent receives structured data instead of making slow MCP calls itself.

For each changed file in To Do/Flagged Email/:
1. Reads frontmatter (emailid, conversationId, title, from, received)
2. Fetches the email thread via Mail MCP
3. Checks To Do/Active/ for an existing item with the same thread
4. Outputs structured JSON for the agent

If MCP fails, still passes the raw frontmatter — the agent can fall back
to its own MCP calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import msal

# ── MCP config (shared with flagged-email-monitor-prep.py) ────────
MCP_MAIL_URL = (
    "https://agent365.svc.cloud.microsoft/agents/tenants/"
    "72f988bf-86f1-41af-91ab-2d7cd011db47/servers/mcp_MailTools"
)
CLIENT_ID = "aebc6443-996d-45c2-90f0-388ff96faa56"
AUTHORITY = "https://login.microsoftonline.com/72f988bf-86f1-41af-91ab-2d7cd011db47"
SCOPES_MAIL = [f"{MCP_MAIL_URL}/.default"]

CACHE_PATH = Path(
    os.environ.get(
        "WORKIQ_MAIL_TOKEN_CACHE_PATH",
        str(Path.home() / ".config" / "workiq-mail" / ".token-cache.json"),
    )
).expanduser()


def log(msg: str) -> None:
    print(f"[flagged-email-triage-prep] {msg}", file=sys.stderr)


def acquire_token(scopes: list[str]) -> str:
    if not CACHE_PATH.is_file():
        raise RuntimeError(f"Token cache not found: {CACHE_PATH}")
    cache = msal.SerializableTokenCache()
    cache.deserialize(CACHE_PATH.read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)
    accounts = app.get_accounts()
    if not accounts:
        raise RuntimeError("No cached accounts")
    result = app.acquire_token_silent(scopes, account=accounts[0])
    if not result or "access_token" not in result:
        raise RuntimeError(f"Token acquisition failed: {result}")
    if cache.has_state_changed:
        CACHE_PATH.write_text(cache.serialize(), encoding="utf-8")
    return result["access_token"]


def extract_tool_text(result: Any) -> str:
    for item in result.content:
        if hasattr(item, "text"):
            return item.text
    return ""


async def call_mcp_tool(url: str, scopes: list[str], tool_name: str, args: dict) -> str:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    token = acquire_token(scopes)
    http_client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(30, read=120),
    )
    for name in ("mcp.client.streamable_http", "mcp"):
        logging.getLogger(name).setLevel(logging.ERROR)

    try:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, args)
                return extract_tool_text(result)
    except Exception as exc:
        log(f"MCP call {tool_name} failed: {exc}")
        raise


def _html_to_markdown(html: str) -> str:
    import html2text
    h = html2text.HTML2Text()
    h.ignore_images = True
    h.body_width = 0
    h.ignore_links = False
    h.unicode_snob = True
    h.single_line_break = True
    text = h.handle(html)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_frontmatter(text: str) -> dict[str, str]:
    fm: dict[str, str] = {}
    if not text.startswith("---"):
        return fm
    end = text.find("\n---", 3)
    if end == -1:
        return fm
    for line in text[4:end].splitlines():
        m = re.match(r'^([\w-]+):\s*(.*)', line)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fm


def find_changed_files(root: Path) -> list[Path]:
    """Find flagged email files. Uses CHANGED_FILES env var if set by watcher."""
    changed = os.environ.get("CHANGED_FILES", "")
    if changed:
        paths = []
        for f in changed.split("\n"):
            f = f.strip()
            if f and not f.startswith("_"):
                p = root / f
                if p.is_file() and p.suffix == ".md":
                    paths.append(p)
        return paths
    # Fallback: all non-index files
    flagged_dir = root / "To Do" / "Flagged Email"
    if not flagged_dir.is_dir():
        return []
    return [f for f in sorted(flagged_dir.glob("*.md"))
            if not f.name.startswith("_")]


def find_existing_active(root: Path, conversation_id: str, emailid: str,
                          subject: str) -> str | None:
    """Check To Do/Active/ for an existing item matching this thread."""
    active_dir = root / "To Do" / "Active"
    if not active_dir.is_dir():
        return None
    # Normalize subject for comparison
    norm_subject = re.sub(
        r'^(Re:|Fw:|Fwd:|Automatic reply:|\[EXTERNAL\])\s*', '',
        subject, flags=re.IGNORECASE
    ).strip().lower()

    for md_file in active_dir.glob("*.md"):
        if md_file.name.startswith("_"):
            continue
        text = md_file.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        if conversation_id and fm.get("conversationId") == conversation_id:
            return str(md_file.relative_to(root))
        if emailid and fm.get("emailid") == emailid:
            return str(md_file.relative_to(root))
        # Subject match
        active_subject = re.sub(
            r'^(Re:|Fw:|Fwd:|Automatic reply:|\[EXTERNAL\]|Action Required -|Monitoring -)\s*',
            '', fm.get("title", ""), flags=re.IGNORECASE
        ).strip().lower()
        if norm_subject and active_subject and norm_subject == active_subject:
            return str(md_file.relative_to(root))
    return None


async def fetch_thread_preview(conversation_id: str) -> list[dict]:
    """Fetch email thread messages (preview only — fast)."""
    if not conversation_id:
        return []
    try:
        query = (f"?$filter=conversationId eq '{conversation_id}'"
                 "&$select=id,receivedDateTime,from,subject,bodyPreview")
        raw = await call_mcp_tool(MCP_MAIL_URL, SCOPES_MAIL,
                                  "SearchMessagesQueryParameters",
                                  {"queryParameters": query})
        parsed = json.loads(raw)
        raw_resp = parsed.get("rawResponse", raw)
        if isinstance(raw_resp, str):
            raw_resp = json.loads(raw_resp)
        values = raw_resp if isinstance(raw_resp, list) else raw_resp.get("value", [])
        messages = []
        for msg in values:
            from_field = msg.get("from", {})
            if isinstance(from_field, dict):
                from_addr = (from_field.get("emailAddress") or {}).get("address", "")
                from_name = (from_field.get("emailAddress") or {}).get("name", "")
            else:
                from_addr = str(from_field)
                from_name = ""
            messages.append({
                "id": msg.get("id", ""),
                "receivedDateTime": msg.get("receivedDateTime", ""),
                "from_name": from_name,
                "from_address": from_addr,
                "subject": msg.get("subject", ""),
                "bodyPreview": msg.get("bodyPreview", ""),
            })
        messages.sort(key=lambda m: m.get("receivedDateTime", ""))
        return messages
    except Exception as exc:
        log(f"thread fetch failed: {exc}")
        return []


async def fetch_full_body(msg_id: str) -> str | None:
    """Fetch the full body of a single email message."""
    try:
        raw = await call_mcp_tool(MCP_MAIL_URL, SCOPES_MAIL,
                                  "GetMessage", {"id": msg_id})
        if not raw:
            return None
        parsed = json.loads(raw)
        data = parsed.get("data", parsed)
        if isinstance(data, str):
            data = json.loads(data)
        body = data.get("body", {})
        if isinstance(body, dict):
            content = body.get("content", "")
            if body.get("contentType", "text").lower() == "html":
                return _html_to_markdown(content)
            return content.strip()
        if isinstance(body, str) and body.lstrip().startswith("<"):
            return _html_to_markdown(body)
        return str(body).strip() if body else None
    except Exception as exc:
        log(f"body fetch failed for {msg_id[:20]}...: {exc}")
        return None


async def process_file(root: Path, filepath: Path) -> dict | None:
    """Process a single flagged email file and return structured context."""
    text = filepath.read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    emailid = fm.get("emailid", "")
    conversation_id = fm.get("conversationId", "")
    title = fm.get("title", "")
    sender = fm.get("from", "")
    received = fm.get("received", "")

    if not emailid and not conversation_id:
        log(f"  {filepath.name}: no emailid or conversationId — skipping")
        return None

    rel_path = str(filepath.relative_to(root))
    log(f"  processing: {rel_path}")

    # Check for existing Active item
    existing_active = find_existing_active(root, conversation_id, emailid, title)
    if existing_active:
        log(f"  existing active item: {existing_active}")

    # Fetch thread
    thread_messages = await fetch_thread_preview(conversation_id)
    log(f"  thread: {len(thread_messages)} messages")

    # Fetch full body of the triggering email
    trigger_body = None
    if emailid:
        trigger_body = await fetch_full_body(emailid)
        if trigger_body:
            log(f"  trigger body: {len(trigger_body)} chars")

    return {
        "source_file": rel_path,
        "frontmatter": fm,
        "existing_active_item": existing_active,
        "thread_message_count": len(thread_messages),
        "thread_messages": thread_messages,
        "trigger_body": trigger_body,
        "mcp_available": True,
    }


async def async_main() -> int:
    root = Path.cwd()
    changed_files = find_changed_files(root)
    if not changed_files:
        log("no changed files to process")
        print(json.dumps({"skip": True, "reason": "no flagged email files to process"}))
        return 0

    log(f"processing {len(changed_files)} file(s)")
    results = []
    for filepath in changed_files:
        result = await process_file(root, filepath)
        if result:
            results.append(result)

    if not results:
        log("no processable files")
        print(json.dumps({"skip": True, "reason": "no files with emailid/conversationId"}))
        return 0

    output = {
        "skip": False,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "files": results,
    }
    print(json.dumps(output))
    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
