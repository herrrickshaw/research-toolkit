#!/bin/bash
# Wrapper for launchd (com.user.pdftomarkdown.plist). Plain script, no LLM
# calls — extraction is pure markitdown, so this costs no agent tokens to
# run daily. One capped batch per invocation; see pdf_to_markdown.py for
# the caps and why they exist.
set -euo pipefail
cd /Users/umashankar/research-toolkit
# Catalog any new documents in the inbox folders first, so they are converted below
# (a discovery failure must not block conversion of what is already catalogued).
/bin/bash /Users/umashankar/research-toolkit/discover_daily.sh >> daily_pdf_to_md.log 2>&1 \
  || echo "discover_daily.sh failed $(date) — continuing to convert" >> daily_pdf_to_md.log
# -u: unbuffered stdout, so the log shows live progress instead of nothing
# until the process exits (Python fully buffers stdout when it's not a tty).
/opt/homebrew/bin/python3 -u pdf_to_markdown.py >> daily_pdf_to_md.log 2>&1
