#!/bin/bash
# Daily insights run (launchd com.user.researchinsights, 08:15 — after the 05:30 convert
# job and the 07:10 `qmd update && qmd embed` cron). No LLM calls.
# Builds research_insights.duckdb + report_latest.md + digest_<date>.md, then
# `rclone copy` (NOT sync — never deletes remote history) to dropbox:research_insights,
# a prefix no other job mirrors.
set -uo pipefail
cd /Users/umashankar/research-toolkit/insights
exec >> run.log 2>&1
echo "=== insights run $(date) ==="
/Users/umashankar/.venvs/research-insights/bin/python -W ignore -u build_insights.py || { echo "BUILD FAILED $(date)"; exit 1; }
/opt/homebrew/bin/rclone copy . dropbox:research_insights \
  --include "research_insights.duckdb" --include "report_latest.md" --include "digest_*.md" --include "README.md" \
  || { echo "UPLOAD FAILED $(date)"; exit 1; }
echo "=== insights run done $(date) ==="
