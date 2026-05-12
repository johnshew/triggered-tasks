#!/bin/bash
# Generic handler: reads JSON from stdin, writes each file in the "files" array.
# Usage: claude -p "..." --permission-mode plan | ./Agents/handlers/write-files.sh
#
# Expected JSON input:
# {
#   "files": [
#     { "path": "relative/path/to/file.md", "content": "file content" }
#   ],
#   "summary": "one-line description of what was produced"
# }

set -euo pipefail

INPUT=$(cat)

# Validate JSON
if ! echo "$INPUT" | jq empty 2>/dev/null; then
  echo "[handler] ERROR: input is not valid JSON" >&2
  echo "[handler] Raw input (first 500 chars):" >&2
  echo "$INPUT" | head -c 500 >&2
  exit 1
fi

# Check for files array
FILE_COUNT=$(echo "$INPUT" | jq '.files | length // 0' 2>/dev/null)
if [ "$FILE_COUNT" -eq 0 ]; then
  echo "[handler] WARNING: no files in output" >&2
  echo "$INPUT" | jq -r '.summary // "No summary provided"' >&2
  exit 0
fi

# Write each file
echo "$INPUT" | jq -c '.files[]' | while IFS= read -r entry; do
  path=$(echo "$entry" | jq -r '.path')
  content=$(echo "$entry" | jq -r '.content')

  if [ -z "$path" ] || [ "$path" = "null" ]; then
    echo "[handler] WARNING: skipping entry with no path" >&2
    continue
  fi

  # Block absolute paths and parent-directory traversal
  if [[ "$path" == /* ]] || [[ "$path" == ../* ]] || [[ "$path" == */../* ]] || [[ "$path" == */.. ]]; then
    echo "[handler] ERROR: rejecting unsafe path: $path" >&2
    continue
  fi

  mkdir -p "$(dirname "$path")"
  printf '%s\n' "$content" > "$path"
  echo "[handler] wrote: $path" >&2
done

summary=$(echo "$INPUT" | jq -r '.summary // empty')
[ -n "$summary" ] && echo "[handler] $summary" >&2

exit 0
