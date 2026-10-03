#!/bin/bash
# One-off driver for "run until the whole backfill is done" (requested
# 2026-09-23). Loops the same capped-batch script the daily launchd job
# uses, back-to-back, until nothing is pending. Safe to kill any time —
# next invocation (this driver, or tomorrow's 05:30 launchd run) resumes
# exactly where it left off via fulltext_md.
set -uo pipefail
cd /Users/umashankar/research-toolkit
LOG=full_backfill_run.log
echo "=== full backfill driver started $(date) ===" >> "$LOG"
while true; do
  remaining=$(/opt/homebrew/bin/python3 -c "
import duckdb
con = duckdb.connect('document_catalog.duckdb', read_only=True)
total = con.execute('select count(*) from documents where is_duplicate=false').fetchone()[0]
done = con.execute('select count(*) from fulltext_md').fetchone()[0]
print(total - done)
")
  if [ "$remaining" -le 0 ]; then
    echo "=== backfill complete $(date) ===" >> "$LOG"
    launchctl bootstrap gui/$(id -u) /Users/umashankar/Library/LaunchAgents/com.user.pdftomarkdown.plist >> "$LOG" 2>&1
    echo "=== daily 05:30 job re-enabled ===" >> "$LOG"
    break
  fi
  echo "--- batch start $(date), $remaining remaining ---" >> "$LOG"
  /opt/homebrew/bin/python3 -u pdf_to_markdown.py --max-docs 1000 --max-mb 8000 >> "$LOG" 2>&1
  sleep 5
done
