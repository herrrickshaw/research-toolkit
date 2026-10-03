#!/bin/bash
# Daily incremental discovery: catalog any NEW convertible documents in the Dropbox
# inbox folders listed in inbox_folders.txt, so the 05:30 pdf_to_markdown run converts
# them and the qmd update picks the .md up. Idempotent (discover_folder_files.py skips
# anything already catalogued by exact path) and cheap (~1-70s per folder).
# --docs-only: skip images/video/installers (a first test run without it catalogued
# 3,232 jpg/mp4/dmg rows, which had to be removed again).
#
# NEVER list research_md_corpus / qmd_cloud_backup / research_insights here: they are
# this pipeline's own output folders. Sweeping research_md_corpus once put 8,461
# copy-backs into the catalog (63% of the corpus ended up as exact duplicates).
#
# It also flags top-level Dropbox folders that appeared since the last run, so a new
# dumping ground gets noticed instead of silently skipped.
set -uo pipefail
cd /Users/umashankar/research-toolkit
PY=/opt/homebrew/bin/python3
RCLONE=/opt/homebrew/bin/rclone
echo "=== discover_daily $(date) ==="
while IFS= read -r folder; do
  case "$folder" in ''|\#*) continue ;; esac
  echo "--- dropbox:$folder"
  "$PY" discover_folder_files.py --docs-only "dropbox:$folder" 2>&1 | tail -3
done < inbox_folders.txt

echo "--- new top-level folder check"
"$RCLONE" lsf dropbox: --dirs-only 2>/dev/null | sed 's#/$##' | sort > /tmp/dbx_top.$$ || true
"$PY" check_new_folders.py /tmp/dbx_top.$$
rm -f /tmp/dbx_top.$$
echo "=== discover_daily done $(date) ==="
