---
agent: none
post-processor: write-files.sh
schedule: "0 9 * * *"
---

# Daily Report Generator

Collect data from the configured sources and produce a summary report.

## Output
Respond with ONLY a JSON object:
{
  "files": [
    { "path": "Reports/daily-report.md", "content": "# Daily Report\n\n..." }
  ],
  "summary": "Generated daily report"
}
