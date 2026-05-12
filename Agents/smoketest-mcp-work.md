---
agent: agency copilot
mode: plan
model: claude-sonnet-4.6
timeout: 60
pre-processor: smoketest-mcp-work-prep.py
schedule: "0 */12 * * *"
mcps:
  - workiq-cli
  - WorkIQ-Mail-MCP-Server
  - WorkIQ-Teams-MCP-Server
  - WorkIQ-Calendar-MCP-Server
---

# Smoketest: Work MCP Liveness

Every 12 hours, confirms that the work-envelope MCP dispatch pipeline is
functional. Two independent paths are tested:
1. Handler path: pre-processor calls ask_work_iq directly via stdio MCP
2. Agent path: Copilot CLI calls ask_work_iq through its MCP integration

Both results are reported. Either can fail independently.

## Instructions

Call `ask_work_iq` with the question: "What is today's date?"

If you get a response, print:

```
AGENT_PATH: PASS - ask_work_iq responded
```

If the tool is unavailable or errors, print:

```
AGENT_PATH: FAIL - <error description>
```

Then check the pre-processor field for `handler_result`. Print:

```
HANDLER_PATH: <PASS or FAIL>
```

Finally, if both passed:
```
SMOKETEST_RESULT: PASS - both paths working
```

If either failed:
```
SMOKETEST_RESULT: FAIL - <which path(s) failed>
```

Do not do anything else. Do not search files or read the codebase.
