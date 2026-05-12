---
agent: agency copilot
mode: plan
pre-processor: my-prep.py
post-processor: write-files.sh
schedule: "0 */4 * * *"
timeout: 300
---

# Agent-Powered Analysis

Analyze the data prepared by the pre-processor and produce a report.

## Instructions

1. Read the pre-processor output for current data
2. Analyze trends and anomalies
3. Produce a summary report

## Output
Respond with ONLY a JSON object:
{
  "files": [
    { "path": "Reports/analysis.md", "content": "# Analysis\n\n..." }
  ],
  "summary": "Completed analysis"
}
