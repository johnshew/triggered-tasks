#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["msal", "httpx", "mcp", "html2text"]
# ///
"""Pre-processor for flagged-email-monitor.

For each email-sourced to-do item in To Do/Active/:
1. Fetches the email thread from Mail MCP (if stale)
2. Fetches related Teams activity from Teams MCP (if stale)
3. Appends only NEW items to the item's context file
4. Emits item paths + context file paths on stdout for the agent

Cache structure (flat files under Agents/data/flagged-email/):
    <emailid>.md  — single living document per tracked email

context.md format:
    ## Metadata        — emailid, conversationId, fetch timestamps
    ## Index           — one line per item (ID, from, date, snippet)
    ## E:<emailid>     — full detail for each email message
    ## T:<messageid>   — full detail for each Teams message

The pre-processor only appends. The agent can annotate (add summaries
to Index entries, add notes sections). Stale files (>7 days since last
fetch) are purged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import msal

# Cache module — imported lazily so failures don't block the handler
_cache_db = None
def _open_cache():
    global _cache_db
    if _cache_db is not None:
        return _cache_db
    try:
        from flagged_email_cache import open_cache
        _cache_db = open_cache()
        return _cache_db
    except Exception as exc:
        log(f"cache: failed to open ({exc}), proceeding without cache")
        return None

# ── MCP server config ──────────────────────────────────────────────
MCP_MAIL_URL = (
    "https://agent365.svc.cloud.microsoft/agents/tenants/"
    "72f988bf-86f1-41af-91ab-2d7cd011db47/servers/mcp_MailTools"
)
MCP_TEAMS_URL = (
    "https://agent365.svc.cloud.microsoft/agents/tenants/"
    "72f988bf-86f1-41af-91ab-2d7cd011db47/servers/mcp_TeamsServer"
)
CLIENT_ID = "aebc6443-996d-45c2-90f0-388ff96faa56"
AUTHORITY = "https://login.microsoftonline.com/72f988bf-86f1-41af-91ab-2d7cd011db47"
SCOPES_MAIL = [f"{MCP_MAIL_URL}/.default"]
SCOPES_TEAMS = [f"{MCP_TEAMS_URL}/.default"]

CACHE_PATH = Path(
    os.environ.get(
        "WORKIQ_MAIL_TOKEN_CACHE_PATH",
        str(Path.home() / ".config" / "workiq-mail" / ".token-cache.json"),
    )
).expanduser()

CACHE_DIR = Path("Agents/data/flagged-email")
CACHE_MAX_AGE_DAYS = 7
REFETCH_INTERVAL_S = 3600  # skip re-fetch if cached < 1h ago

TOPIC_STOP_WORDS = {
    "the", "and", "for", "from", "with", "this", "that", "about",
    "external", "automatic", "reply",
}


def log(msg: str) -> None:
    print(f"[flagged-email-monitor-prep] {msg}", file=sys.stderr)


def log_step_start(name: str, detail: str = "") -> float:
    started = time.monotonic()
    suffix = f" {detail}" if detail else ""
    log(f"{name}: start{suffix}")
    return started


def log_step_end(name: str, started: float, detail: str = "") -> None:
    suffix = f" {detail}" if detail else ""
    log(f"{name}: end ({time.monotonic() - started:.1f}s){suffix}")


# ── Token auth ─────────────────────────────────────────────────────
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


# ── MCP helpers ────────────────────────────────────────────────────
def extract_tool_text(result: Any) -> str:
    for item in result.content:
        if hasattr(item, "text"):
            return item.text
    return ""


def walk_exceptions(exc: BaseException) -> list[BaseException]:
    """Flatten nested exception groups/causes for inspection."""
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    flattened: list[BaseException] = []
    while pending:
        current = pending.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        flattened.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(reversed(current.exceptions))
        cause = current.__cause__
        if isinstance(cause, BaseException):
            pending.append(cause)
        context = current.__context__
        if isinstance(context, BaseException):
            pending.append(context)
    return flattened


def is_transient_mcp_failure(exc: BaseException) -> bool:
    """Return True for temporary MCP/HTTP failures worth retrying/skipping."""
    import httpx

    for current in walk_exceptions(exc):
        if isinstance(current, json.JSONDecodeError):
            return True
        if isinstance(current, (httpx.TimeoutException, httpx.NetworkError)):
            return True
        if isinstance(current, httpx.HTTPStatusError):
            status = current.response.status_code
            if status == 429 or status >= 500:
                return True
    return False


def summarize_exception(exc: BaseException) -> str:
    """Return the most useful nested exception message."""
    for current in walk_exceptions(exc):
        if current is not exc:
            return str(current) or current.__class__.__name__
    return str(exc) or exc.__class__.__name__


async def call_mcp_tool(url: str, scopes: list[str], tool_name: str, args: dict) -> Any:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    token = acquire_token(scopes)
    http_client = httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(30, read=300),
    )
    for name in ("mcp.client.streamable_http", "mcp"):
        logging.getLogger(name).setLevel(logging.ERROR)

    started = time.monotonic()
    args_summary = {k: (v[:80] + "...") if isinstance(v, str) and len(v) > 80 else v
                    for k, v in args.items()}
    log(f"MCP {tool_name}: start {json.dumps(args_summary, sort_keys=True)}")

    try:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, args)
                text = extract_tool_text(result)
                log(f"MCP {tool_name}: end ({time.monotonic() - started:.1f}s, {len(text)} chars)")
                return text
    except Exception as exc:
        detail = summarize_exception(exc)
        if is_transient_mcp_failure(exc):
            log(f"MCP {tool_name}: transient failure ({time.monotonic() - started:.1f}s) {detail}")
        else:
            log(f"MCP {tool_name}: failed ({time.monotonic() - started:.1f}s) {detail}")
        raise


# ── Frontmatter parsing ───────────────────────────────────────────
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


# ── Context file management ───────────────────────────────────────
def context_file_path(root: Path, emailid: str) -> Path:
    return root / CACHE_DIR / f"{emailid}.md"


def read_context_file(path: Path) -> str:
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return ""


def parse_context_metadata(text: str) -> dict[str, str]:
    """Parse the ## Metadata section of a context file."""
    meta: dict[str, str] = {}
    in_meta = False
    for line in text.splitlines():
        if line.strip() == "## Metadata":
            in_meta = True
            continue
        if in_meta and line.startswith("## "):
            break
        if not in_meta:
            continue
        m = re.match(r'^- ([\w-]+): (.+)', line)
        if m:
            meta[m.group(1)] = m.group(2).strip()
    return meta


def parse_known_ids(text: str) -> set[str]:
    """Extract all item IDs from ## detail headings."""
    ids: set[str] = set()
    for line in text.splitlines():
        # Detail headings: "## E:AAMk..." or "## T:12345"
        m = re.match(r'^## (E:\S+|T:\S+)', line)
        if m:
            ids.add(m.group(1))
    return ids


def is_fetch_fresh(text: str, field: str) -> bool:
    """Check if a fetch timestamp in metadata is within the refetch interval."""
    meta = parse_context_metadata(text)
    ts_str = meta.get(field)
    if not ts_str or ts_str == "never":
        return False
    try:
        fetched = datetime.fromisoformat(ts_str)
        return (datetime.now(timezone.utc) - fetched).total_seconds() < REFETCH_INTERVAL_S
    except ValueError:
        return False


def update_metadata_field(text: str, field: str, value: str) -> str:
    """Update a single field in the ## Metadata section."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf'^- {re.escape(field)}: ', line):
            lines[i] = f"- {field}: {value}"
            return "\n".join(lines) + "\n"
    # Field not found — insert before end of Metadata section
    for i, line in enumerate(lines):
        if line.strip() == "## Metadata":
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("## "):
                    lines.insert(j, f"- {field}: {value}")
                    return "\n".join(lines) + "\n"
            lines.append(f"- {field}: {value}")
            return "\n".join(lines) + "\n"
    return text


def create_initial_context(emailid: str, conversation_id: str, item_path: str) -> str:
    """Create the initial context file content."""
    now = datetime.now(timezone.utc).isoformat()
    return (
        f"## Metadata\n"
        f"- emailid: {emailid}\n"
        f"- conversationId: {conversation_id}\n"
        f"- item: {item_path}\n"
        f"- email-last-fetched: never\n"
        f"- teams-last-fetched: never\n"
        f"- created: {now}\n"
        f"\n"
        f"## Index\n"
        f"\n"
    )


def find_index_end(lines: list[str]) -> int:
    """Find the line number where ## Index section ends (next ## or EOF)."""
    in_index = False
    for i, line in enumerate(lines):
        if line.strip() == "## Index":
            in_index = True
            continue
        if in_index and line.startswith("## "):
            return i
    return len(lines)


def append_to_context(text: str, entry_id: str, index_summary: str,
                      detail_lines: list[str]) -> str:
    """Append an index entry and detail section to the context file."""
    lines = text.rstrip("\n").split("\n")

    # Insert index entry at end of ## Index section
    insert_at = find_index_end(lines)
    lines.insert(insert_at, f"- {entry_id} — {index_summary}")

    # Append detail section at end
    lines.append("")
    lines.append(f"## {entry_id}")
    lines.extend(detail_lines)

    return "\n".join(lines) + "\n"


# ── Email thread fetch ────────────────────────────────────────────
async def fetch_email_thread(conversation_id: str,
                              since_date: str | None = None) -> list[dict]:
    """Fetch email messages in a conversation thread.

    If *since_date* is provided (ISO-8601 string from the DB cache), only
    messages newer than that date are fetched.  This avoids re-downloading
    the full thread on every stale run.  Falls back to a full fetch
    automatically when *since_date* is None (first run or empty cache).
    """
    started = log_step_start(
        "email thread",
        f"conversation={conversation_id[:20]} mode={'incremental' if since_date else 'full'}",
    )
    all_messages: list[dict] = []
    skip = 0
    while True:
        query = f"?$filter=conversationId eq '{conversation_id}'"
        if since_date:
            query += f" AND receivedDateTime gt '{since_date}'"
        query += "&$select=id,receivedDateTime,from,subject,bodyPreview"
        if skip > 0:
            query += f"&$skip={skip}"

        raw = await call_mcp_tool(MCP_MAIL_URL, SCOPES_MAIL,
                                  "SearchMessagesQueryParameters",
                                  {"queryParameters": query})
        parsed = json.loads(raw)
        raw_resp = parsed.get("rawResponse", raw)
        if isinstance(raw_resp, str):
            raw_resp = json.loads(raw_resp)

        values = raw_resp if isinstance(raw_resp, list) else raw_resp.get("value", [])
        for msg in values:
            from_field = msg.get("from", {})
            if isinstance(from_field, dict):
                from_addr = (from_field.get("emailAddress") or {}).get("address", "")
                from_name = (from_field.get("emailAddress") or {}).get("name", "")
            else:
                from_addr = str(from_field)
                from_name = ""

            all_messages.append({
                "id": msg.get("id", ""),
                "receivedDateTime": msg.get("receivedDateTime", ""),
                "from_name": from_name,
                "from_address": from_addr,
                "subject": msg.get("subject", ""),
                "bodyPreview": msg.get("bodyPreview", ""),
            })

        has_next = raw_resp.get("@odata.nextLink") if isinstance(raw_resp, dict) else None
        if not has_next or not values:
            break
        skip += len(values)

    all_messages.sort(key=lambda m: m.get("receivedDateTime", ""))
    kind = "incremental" if since_date else "full"
    log_step_end("email thread", started, f"{kind} fetch returned {len(all_messages)} message(s)")

    # Cache fetched emails
    db = _open_cache()
    if db:
        try:
            from flagged_email_cache import bulk_upsert_messages
            bulk_upsert_messages(db, [
                {
                    "id": m["id"],
                    "type": "email",
                    "conversation_id": conversation_id,
                    "from_name": m["from_name"],
                    "from_address": m["from_address"],
                    "date": m["receivedDateTime"],
                    "subject": m["subject"],
                    "body_preview": m["bodyPreview"],
                }
                for m in all_messages
            ])
            log(f"cache: stored {len(all_messages)} email messages")
        except Exception as exc:
            log(f"cache: write failed ({exc}), continuing")

    return all_messages


def _html_to_markdown(html: str) -> str:
    """Convert Outlook/Exchange HTML email body to clean markdown via html2text."""
    import html2text
    h = html2text.HTML2Text()
    h.ignore_images = True       # skip tracking pixels, logos
    h.body_width = 0             # no line wrapping
    h.ignore_links = False       # keep mailto: and URLs
    h.unicode_snob = True        # use unicode chars instead of ASCII
    h.single_line_break = True   # Outlook uses <br> not <p> heavily
    text = h.handle(html)
    # Collapse 3+ blank lines to 2
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


async def fetch_message_body(msg_id: str) -> str | None:
    """Fetch the full body of a single email message via GetMessage.

    Returns clean markdown (HTML converted via html2text) or None on failure.
    """
    try:
        started = time.monotonic()
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
            content_type = body.get("contentType", "text")
            if content_type.lower() == "html":
                return _html_to_markdown(content)
            return content.strip()
        # body might be a raw HTML string (no contentType wrapper)
        if isinstance(body, str) and body.lstrip().startswith("<"):
            return _html_to_markdown(body)
        return str(body).strip() if body else None
    except Exception as exc:
        log(f"GetMessage body fetch failed for {msg_id[:20]}...: {exc} ({time.monotonic() - started:.1f}s)")
        return None


async def fetch_full_bodies(emails: list[dict], trigger_id: str) -> dict[str, str]:
    """Fetch full bodies for the triggering email + latest 3 non-trigger messages.

    Returns {msg_id: body_text} for messages that were successfully fetched.
    """
    # Determine which messages need full bodies
    ids_to_fetch: list[str] = []
    if trigger_id:
        ids_to_fetch.append(trigger_id)

    # Latest 3 non-trigger messages (by date descending)
    sorted_msgs = sorted(emails, key=lambda m: m.get("receivedDateTime", ""),
                         reverse=True)
    for msg in sorted_msgs:
        msg_id = msg.get("id", "")
        if not msg_id:
            continue
        if msg_id != trigger_id and msg_id not in ids_to_fetch:
            ids_to_fetch.append(msg_id)
            if len(ids_to_fetch) >= 4:  # trigger + 3
                break

    # Check DB cache first
    bodies: dict[str, str] = {}
    db = _open_cache()
    if db:
        try:
            from flagged_email_cache import get_message
            for mid in ids_to_fetch:
                cached = get_message(db, mid)
                if cached and cached.get("body_text"):
                    bodies[mid] = cached["body_text"]
        except Exception:
            pass

    # Fetch any that aren't cached
    uncached = [mid for mid in ids_to_fetch if mid not in bodies]
    if uncached:
        log(f"full bodies: fetching {len(uncached)} message(s) ({len(bodies)} from cache)")
        for mid in uncached:
            body = await fetch_message_body(mid)
            if body:
                bodies[mid] = body
                # Cache in DB
                if db:
                    try:
                        db.execute(
                            "UPDATE messages SET body_text = ? WHERE id = ?",
                            (body, mid))
                        db.commit()
                    except Exception:
                        pass
    else:
        log(f"full bodies: all {len(bodies)} from cache (0 MCP calls)")

    return bodies


# ── Teams activity fetch ──────────────────────────────────────────
def _extract_topic_terms(emails: list[dict], max_terms: int = 6) -> set[str]:
    """Extract meaningful topic terms from email subjects."""
    terms: set[str] = set()
    for msg in emails:
        subj = re.sub(r'^(RE|FW|Fwd):\s*', '', msg.get("subject", ""), flags=re.IGNORECASE)
        for word in subj.split():
            word = re.sub(r'[^\w]', '', word)
            if len(word) > 3 and word.lower() not in TOPIC_STOP_WORDS:
                terms.add(word.lower())
                if len(terms) >= max_terms:
                    return terms
    return terms


def build_teams_kql(emails: list[dict]) -> str:
    """Build a KQL query from email thread subjects."""
    terms = _extract_topic_terms(emails)
    if not terms:
        return ""

    dates = [m["receivedDateTime"][:10] for m in emails if m.get("receivedDateTime")]
    if dates:
        start_dt = datetime.fromisoformat(min(dates)) - timedelta(days=7)
        end_dt = datetime.fromisoformat(max(dates)) + timedelta(days=7)
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=30)

    terms_str = " OR ".join(f'"{t}"' for t in sorted(terms))
    return f"({terms_str}) sent>={start_dt.strftime('%Y-%m-%d')} sent<={end_dt.strftime('%Y-%m-%d')}"


async def fetch_teams_activity(emails: list[dict],
                               known_chat_ids: list[str]) -> list[dict]:
    """Fetch Teams messages related to the email thread."""
    started = log_step_start("teams activity", f"emails={len(emails)} chats={len(known_chat_ids)}")
    topic_terms = _extract_topic_terms(emails)

    kql = build_teams_kql(emails)
    if not kql:
        log_step_end("teams activity", started, "no search terms extracted; skipping")
        return []

    relevant_chat_ids: set[str] = set(known_chat_ids)
    kql_hit_ids: set[str] = set()

    try:
        raw = await call_mcp_tool(MCP_TEAMS_URL, SCOPES_TEAMS,
                                  "SearchTeamMessagesQueryParameters",
                                  {"queryString": kql, "size": 25})
        parsed = json.loads(raw)
        raw_resp = parsed.get("rawResponse", raw)
        if isinstance(raw_resp, str):
            raw_resp = json.loads(raw_resp)

        email_participants = {m.get("from_name", "").lower() for m in emails}
        for container in raw_resp.get("value", []):
            for hit in container.get("hitsContainers", [container]):
                for h in hit.get("hits", []):
                    resource = h.get("resource", {})
                    chat_id = resource.get("chatId", "")
                    msg_id = resource.get("id", "")
                    sender = resource.get("from", {})
                    sender_name = ""
                    if isinstance(sender, dict):
                        sender_name = sender.get("emailAddress", {}).get("name", "")
                    if sender_name.lower() in email_participants or chat_id in relevant_chat_ids:
                        relevant_chat_ids.add(chat_id)
                        if msg_id:
                            kql_hit_ids.add(msg_id)
    except Exception as exc:
        log(f"teams KQL search failed: {exc}")

    # Date range for filtering
    dates = [m["receivedDateTime"][:10] for m in emails if m.get("receivedDateTime")]
    if dates:
        filter_start = (datetime.fromisoformat(min(dates)) - timedelta(days=7)).isoformat() + "Z"
        filter_end = (datetime.fromisoformat(max(dates)) + timedelta(days=7)).isoformat() + "Z"
    else:
        filter_end = datetime.now(timezone.utc).isoformat()
        filter_start = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    all_teams_msgs: list[dict] = []

    for chat_id in sorted(relevant_chat_ids):
        if not chat_id:
            continue
        try:
            raw = await call_mcp_tool(MCP_TEAMS_URL, SCOPES_TEAMS,
                                      "ListChatMessages",
                                      {"chatId": chat_id, "top": "50"})
            data = json.loads(raw)
            messages = data.get("messages", data.get("value", []))
            for msg in messages:
                created = msg.get("createdDateTime", "")
                if not created or created < filter_start or created > filter_end:
                    continue
                msg_type = msg.get("messageType", "")
                if msg_type and msg_type != "message":
                    continue

                sender = msg.get("from", {})
                sender_name = ""
                if isinstance(sender, dict):
                    user = sender.get("user", {})
                    if isinstance(user, dict):
                        sender_name = user.get("displayName", "")

                body = msg.get("body", {})
                content = ""
                if isinstance(body, dict):
                    content = body.get("content", "")
                content_preview = re.sub(r'<[^>]+>', '', content)

                msg_id = msg.get("id", "")

                # Content relevance filter
                if msg_id not in kql_hit_ids:
                    content_lower = content_preview.lower()
                    if not any(t in content_lower for t in topic_terms):
                        continue

                all_teams_msgs.append({
                    "id": msg_id,
                    "chatId": chat_id,
                    "createdDateTime": created,
                    "from_name": sender_name,
                    "contentPreview": content_preview,
                })
        except Exception as exc:
            log(f"teams chat {chat_id[:30]}... fetch failed: {exc}")

    all_teams_msgs.sort(key=lambda m: m.get("createdDateTime", ""))
    log_step_end(
        "teams activity",
        started,
        f"fetched {len(all_teams_msgs)} message(s) from {len(relevant_chat_ids)} chat(s)",
    )

    # Cache fetched Teams messages
    db = _open_cache()
    if db:
        try:
            from flagged_email_cache import bulk_upsert_messages
            bulk_upsert_messages(db, [
                {
                    "id": m["id"],
                    "type": "teams",
                    "conversation_id": m["chatId"],
                    "from_name": m["from_name"],
                    "from_address": "",
                    "date": m["createdDateTime"],
                    "subject": "",
                    "body_preview": m["contentPreview"],
                }
                for m in all_teams_msgs
            ])
            log(f"cache: stored {len(all_teams_msgs)} teams messages")
        except Exception as exc:
            log(f"cache: write failed ({exc}), continuing")

    return all_teams_msgs


# ── Main processing ───────────────────────────────────────────────
def find_email_sourced_items(root: Path) -> list[tuple[str, dict[str, str], str]]:
    """Find all email-sourced to-do items. Returns [(rel_path, frontmatter, full_text)]."""
    items = []
    active_dir = root / "To Do" / "Active"
    if not active_dir.is_dir():
        return items
    for md_file in sorted(active_dir.glob("*.md")):
        if md_file.name.startswith("_"):
            continue
        text = md_file.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        # Match items with the flagged-email marker or that have an emailid
        if fm.get("status") == "completed":
            continue
        if fm.get("flagged-email") or fm.get("emailid"):
            rel = str(md_file.relative_to(root))
            items.append((rel, fm, text))
    return items


def extract_chat_ids_from_context(text: str) -> list[str]:
    """Extract chatId values from existing context file."""
    chat_ids: list[str] = []
    for line in text.splitlines():
        m = re.search(r'chatId: (\S+)', line)
        if m:
            cid = m.group(1)
            if cid not in chat_ids:
                chat_ids.append(cid)
    return chat_ids


def reconstruct_emails_from_context(text: str) -> list[dict]:
    """Reconstruct minimal email list from context Index lines for KQL building."""
    emails: list[dict] = []
    for line in text.splitlines():
        m = re.match(r'^- E:\S+ — (.+?), (\d{4}-\d{2}-\d{2}T[\d:]+), (.+)$', line)
        if m:
            name_addr = m.group(1)
            name = name_addr.split(" (")[0]
            emails.append({
                "from_name": name,
                "from_address": "",
                "receivedDateTime": m.group(2),
                "subject": m.group(3),
            })
    return emails


def purge_stale_context_files(root: Path) -> None:
    """Remove context files older than CACHE_MAX_AGE_DAYS."""
    base = root / CACHE_DIR
    if not base.is_dir():
        return
    cutoff = time.time() - CACHE_MAX_AGE_DAYS * 86400
    for f in base.glob("*.md"):
        if f.name.startswith("_"):
            continue
        if f.stat().st_mtime < cutoff:
            f.unlink()
            log(f"purged stale context: {f.name[:30]}...")


async def process_item(root: Path, rel_path: str, fm: dict[str, str],
                       file_text: str) -> dict | None:
    """Process one email-sourced item. Returns result dict or None."""
    started = log_step_start("item", rel_path)
    emailid = fm.get("emailid", "")
    conversation_id = fm.get("conversationId", "")

    if not emailid and not conversation_id:
        log_step_end("item", started, "no emailid or conversationId; skipping")
        return None

    ctx_path = context_file_path(root, emailid)
    ctx_text = read_context_file(ctx_path)

    # Create context file if it doesn't exist
    if not ctx_text:
        if not conversation_id and emailid:
            log("conversationId: fetching via GetMessage")
            try:
                raw = await call_mcp_tool(MCP_MAIL_URL, SCOPES_MAIL,
                                          "GetMessage",
                                          {"id": emailid, "bodyPreviewOnly": True})
                if raw:
                    parsed = json.loads(raw)
                    # GetMessage returns {message: ..., data: {conversationId: ...}}
                    data = parsed.get("data", parsed)
                    if isinstance(data, str):
                        data = json.loads(data)
                    conversation_id = data.get("conversationId", "")
                    if conversation_id:
                        log(f"conversationId: resolved {conversation_id[:30]}...")
                    else:
                        log("conversationId: message found but no conversationId field")
                else:
                    log("conversationId: GetMessage returned empty response")
            except Exception as exc:
                log(f"conversationId: GetMessage failed: {exc}")

        if not conversation_id:
            log_step_end("item", started, "no conversationId available; skipping")
            return None

        ctx_text = create_initial_context(emailid, conversation_id, rel_path)
        ctx_path.parent.mkdir(parents=True, exist_ok=True)

    # Get conversationId from context metadata if not in frontmatter
    if not conversation_id:
        meta = parse_context_metadata(ctx_text)
        conversation_id = meta.get("conversationId", "")

    if not conversation_id:
        log_step_end("item", started, "no conversationId in context; skipping")
        return None

    known_ids = parse_known_ids(ctx_text)
    known_chat_ids = extract_chat_ids_from_context(ctx_text)

    # Track this item in the cache
    db = _open_cache()
    if db and emailid:
        try:
            from flagged_email_cache import upsert_item
            upsert_item(db, emailid=emailid,
                        conversation_id=conversation_id,
                        item_path=rel_path)
        except Exception as exc:
            log(f"cache: item tracking failed ({exc}), continuing")

    new_email_count = 0
    new_teams_count = 0
    emails: list[dict] = []

    # ── Fetch emails if stale ──
    if not is_fetch_fresh(ctx_text, "email-last-fetched"):
        # Use the latest cached date for an incremental fetch so we avoid
        # re-downloading the full thread on every stale run.
        latest_cached_date: str | None = None
        db = _open_cache()
        if db:
            try:
                from flagged_email_cache import get_latest_date
                latest_cached_date = get_latest_date(db, conversation_id, "email")
            except Exception:
                pass

        try:
            emails = await fetch_email_thread(conversation_id,
                                              since_date=latest_cached_date)
        except Exception as exc:
            if not is_transient_mcp_failure(exc):
                raise
            # Fall back to DB cache if available, then context file
            db = _open_cache()
            if db:
                try:
                    from flagged_email_cache import get_thread_messages
                    cached = get_thread_messages(db, conversation_id, "email")
                    if cached:
                        emails = [
                            {"id": m["id"], "from_name": m["from_name"] or "",
                             "from_address": m["from_address"] or "",
                             "receivedDateTime": m["date"],
                             "subject": m["subject"] or "",
                             "bodyPreview": m["body_preview"] or ""}
                            for m in cached
                        ]
                        log(f"emails: MCP failed, using {len(emails)} cached from DB")
                except Exception:
                    pass
            if not emails:
                emails = reconstruct_emails_from_context(ctx_text)
            if not emails:
                log("emails: transient MCP failure and no cached thread, skipping item")
                return None
            if not any(e.get("bodyPreview") for e in emails):
                log("emails: transient MCP failure, using cached context (metadata only)")
            else:
                log("emails: transient MCP failure, using cached data")
        else:
            now = datetime.now(timezone.utc).isoformat()

            for msg in emails:
                eid = f"E:{msg['id']}"
                if eid in known_ids:
                    continue
                new_email_count += 1
                date_short = msg["receivedDateTime"][:16]
                index_summary = (
                    f"{msg['from_name']} ({msg['from_address']}), "
                    f"{date_short}, {msg['subject']}"
                )
                detail = [
                    f"- from: {msg['from_name']} ({msg['from_address']})",
                    f"- date: {msg['receivedDateTime']}",
                    f"- subject: {msg['subject']}",
                ]
                if msg.get("bodyPreview"):
                    detail.append(f"- preview: {msg['bodyPreview']}")
                ctx_text = append_to_context(ctx_text, eid, index_summary, detail)
                known_ids.add(eid)

            ctx_text = update_metadata_field(ctx_text, "email-last-fetched", now)
            log(f"emails: {new_email_count} new")

            # Incremental fetch may return 0 new messages when the thread is
            # unchanged.  Load the full cached list from DB so Teams KQL can
            # still build a meaningful query.
            if not emails and latest_cached_date:
                db = _open_cache()
                if db:
                    try:
                        from flagged_email_cache import get_thread_messages
                        cached = get_thread_messages(db, conversation_id, "email")
                        if cached:
                            emails = [
                                {"id": m["id"], "from_name": m["from_name"] or "",
                                 "from_address": m["from_address"] or "",
                                 "receivedDateTime": m["date"],
                                 "subject": m["subject"] or "",
                                 "bodyPreview": m["body_preview"] or ""}
                                for m in cached
                            ]
                            log(f"emails: no new since {latest_cached_date[:16]}, "
                                f"loaded {len(emails)} from DB cache for Teams KQL")
                    except Exception:
                        pass
    else:
        log("emails: fresh (skipped)")
        # Prefer DB cache over context-file reconstruction (richer data)
        db = _open_cache()
        if db:
            try:
                from flagged_email_cache import get_thread_messages
                cached = get_thread_messages(db, conversation_id, "email")
                if cached:
                    emails = [
                        {"id": m["id"], "from_name": m["from_name"] or "",
                         "from_address": m["from_address"] or "",
                         "receivedDateTime": m["date"],
                         "subject": m["subject"] or "",
                         "bodyPreview": m["body_preview"] or ""}
                        for m in cached
                    ]
                    log(f"emails: loaded {len(emails)} from DB cache")
            except Exception:
                pass
        if not emails:
            emails = reconstruct_emails_from_context(ctx_text)

    # ── Fetch Teams if stale ──
    if emails and not is_fetch_fresh(ctx_text, "teams-last-fetched"):
        try:
            teams = await fetch_teams_activity(emails, known_chat_ids)
        except Exception as exc:
            teams = []
            if is_transient_mcp_failure(exc):
                log("teams: transient MCP failure, continuing without new Teams data")
            else:
                log(f"teams: fetch failed ({exc}), continuing")
        now = datetime.now(timezone.utc).isoformat()

        for msg in teams:
            tid = f"T:{msg['id']}"
            if tid in known_ids:
                continue
            new_teams_count += 1
            date_short = msg["createdDateTime"][:16]
            index_summary = (
                f"{msg['from_name']}, {date_short}, "
                f"chatId: {msg['chatId']}"
            )
            detail = [
                f"- from: {msg['from_name']}",
                f"- date: {msg['createdDateTime']}",
                f"- chatId: {msg['chatId']}",
                f"- content: {msg['contentPreview']}",
            ]
            ctx_text = append_to_context(ctx_text, tid, index_summary, detail)
            known_ids.add(tid)

        ctx_text = update_metadata_field(ctx_text, "teams-last-fetched", now)
        log(f"teams: {new_teams_count} new")
    else:
        if not emails:
            log("teams: no emails to search from")
        else:
            log("teams: fresh (skipped)")

    # ── Fetch full bodies for trigger + latest 3 ──
    bodies: dict[str, str] = {}
    if emails and emailid:
        bodies = await fetch_full_bodies(emails, emailid)
        if bodies:
            log(f"full bodies: {len(bodies)} message(s)")
            # Update context file detail sections with body text
            for msg_id, body_text in bodies.items():
                eid = f"E:{msg_id}"
                # Add body section after the detail heading if not already present
                body_marker = f"- body:\n"
                if eid in known_ids and body_marker not in ctx_text:
                    # Find the detail section and append body
                    section_header = f"## {eid}\n"
                    pos = ctx_text.find(section_header)
                    if pos >= 0:
                        # Find next section or end
                        next_section = ctx_text.find("\n## ", pos + len(section_header))
                        insert_pos = next_section if next_section >= 0 else len(ctx_text.rstrip())
                        # Truncate body to 2000 chars for context file (full text in DB)
                        body_for_ctx = body_text[:2000]
                        if len(body_text) > 2000:
                            body_for_ctx += "\n... (full body in cache.db)"
                        body_block = f"- body:\n{body_for_ctx}\n"
                        ctx_text = ctx_text[:insert_pos] + "\n" + body_block + ctx_text[insert_pos:]

    # Write context file
    ctx_path.write_text(ctx_text, encoding="utf-8")

    # Get trigger email metadata + body for Active file enrichment
    trigger_body = bodies.get(emailid, "") if emailid else ""
    trigger_meta: dict[str, str] = {}
    if emailid and emails:
        for msg in emails:
            if msg.get("id") == emailid:
                from_name = msg.get("from_name", "")
                from_addr = msg.get("from_address", "")
                # Clean up X500 addresses (e.g. /O=EXCHANGELABS/...)
                if from_addr.startswith("/"):
                    from_str = from_name or from_addr
                else:
                    from_str = f"{from_name} <{from_addr}>" if from_addr else from_name
                trigger_meta = {
                    "from": from_str,
                    "date": msg.get("receivedDateTime", ""),
                    "subject": msg.get("subject", ""),
                }
                break

    ctx_rel = str(ctx_path.relative_to(root))
    log_step_end(
        "item",
        started,
        f"new_emails={new_email_count} new_teams={new_teams_count} context={ctx_rel}",
    )
    return {
        "item": rel_path,
        "context": ctx_rel,
        "new_emails": new_email_count,
        "new_teams": new_teams_count,
        "trigger_body": trigger_body,
        "trigger_meta": trigger_meta,
    }


def _ensure_original_email_body(root: Path, item_path: str,
                                trigger_body: str,
                                trigger_meta: dict[str, str] | None = None) -> bool:
    """Insert the full flagged email body into ## Original Message.

    Appends the body (with headers) at the end of the Original Message section,
    right before the next ## section. Returns True if added.
    """
    if not trigger_body:
        return False
    filepath = root / item_path
    if not filepath.is_file():
        return False
    text = filepath.read_text(encoding="utf-8")

    # Skip if already injected
    MARKER = "<!-- flagged-email-body -->"
    if MARKER in text:
        return False

    # Build header + body block
    header_lines: list[str] = []
    if trigger_meta:
        if trigger_meta.get("from"):
            header_lines.append(f"**From:** {trigger_meta['from']}")
        if trigger_meta.get("date"):
            header_lines.append(f"**Date:** {trigger_meta['date']}")
        if trigger_meta.get("subject"):
            header_lines.append(f"**Subject:** {trigger_meta['subject']}")
    header_block = "  \n".join(header_lines) + "\n\n" if header_lines else ""
    body_block = (
        f"\n\n{MARKER}\n"
        f"{header_block}"
        f"{trigger_body}\n"
    )

    # Try to insert at end of ## Original Message section
    om_match = re.search(r'^## Original Message\b', text, re.MULTILINE)
    if om_match:
        # Find the next ## heading after Original Message
        next_section = re.search(r'^## ', text[om_match.end():], re.MULTILINE)
        if next_section:
            insert_pos = om_match.end() + next_section.start()
            text = text[:insert_pos].rstrip() + body_block + "\n\n" + text[insert_pos:]
        else:
            # No next section — append at end
            text = text.rstrip() + body_block
    else:
        # No Original Message section — append as standalone
        text = text.rstrip() + "\n\n## The Flagged Email\n" + body_block

    filepath.write_text(text, encoding="utf-8")
    log(f"  added flagged email body to {item_path}")
    return True


async def async_main() -> int:
    root = Path.cwd()
    if not (root / ".git").exists():
        log(f"not at repo root: {root}")
        return 1

    (root / CACHE_DIR).mkdir(parents=True, exist_ok=True)
    purge_stale_context_files(root)

    items = find_email_sourced_items(root)
    if not items:
        log("no email-sourced items in To Do/Active/")
        print(json.dumps({"skip": True, "reason": "no email-sourced items"}))
        return 0

    # Skip gate: only invoke the agent if at least one item is pending
    pending_items = [
        (p, fm, t) for p, fm, t in items
        if fm.get("pending", "").lower() in ("true", "yes", "1")
    ]
    if not pending_items:
        log(f"found {len(items)} email-sourced item(s) but none are pending — skipping agent")
        print(json.dumps({
            "skip": True,
            "reason": f"{len(items)} email-sourced items but none have pending: true",
        }))
        return 0

    log(f"found {len(items)} email-sourced item(s) ({len(pending_items)} pending)")

    results: list[dict] = []
    for rel_path, fm, file_text in items:
        log(f"processing: {rel_path}")
        result = await process_item(root, rel_path, fm, file_text)
        if result:
            results.append(result)
            # Enrich Active file with original email body
            _ensure_original_email_body(root, result["item"],
                                        result.get("trigger_body", ""),
                                        result.get("trigger_meta"))

    if not results:
        log("no items with fetchable data")
        print(json.dumps({"skip": True, "reason": "no items with fetchable data"}))
        return 0

    # Emit results for the agent — item paths and context file paths
    output_lines: list[str] = []
    for r in results:
        output_lines.append(f"item: {r['item']}")
        output_lines.append(f"context: {r['context']}")
        output_lines.append(f"new-emails: {r['new_emails']}")
        output_lines.append(f"new-teams: {r['new_teams']}")
        output_lines.append("")
    print("\n".join(output_lines))

    # Log cache stats and close
    db = _open_cache()
    if db:
        try:
            from flagged_email_cache import cache_stats
            stats = cache_stats(db)
            log(f"cache stats: {stats}")
            db.close()
            _cache_db = None
        except Exception:
            pass

    return 0


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
