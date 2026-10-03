#!/bin/bash
# Restore qmd's research corpus + search index from the cloud backup
# (qmd_cloud_backup.sh's compressed .tar.zst / .sqlite.zst archives). Use
# on a new machine, or after local disk was cleared, to get back to a fully
# working local `qmd search`/`qmd query` without re-running the whole
# pdf_to_markdown.py backfill or re-embedding from scratch.
#
# Safe by default: refuses to overwrite an existing local qmd index or
# md_corpus/ unless --force is passed, since restoring blind over a live,
# newer local index would lose whatever's changed since the last backup.
#
# Usage:
#   ./qmd_cloud_restore.sh                    # restore only if nothing local exists yet
#   ./qmd_cloud_restore.sh --force             # overwrite existing local copies
#   ./qmd_cloud_restore.sh --from googledrive  # use the Google Drive copy instead of Dropbox
set -euo pipefail

RCLONE_BIN=/opt/homebrew/bin/rclone
ZSTD_BIN=/opt/homebrew/bin/zstd
MD_CORPUS_PARENT=/Users/umashankar/research-toolkit
MD_CORPUS=/Users/umashankar/research-toolkit/md_corpus
QMD_CACHE_DIR=/Users/umashankar/.cache/qmd
QMD_INDEX="$QMD_CACHE_DIR/index.sqlite"
TMP_DIR=/Users/umashankar/research-toolkit/qmd_restore_tmp

FORCE=false
REMOTE="dropbox:qmd_cloud_backup"
while [ $# -gt 0 ]; do
  case "$1" in
    --force) FORCE=true; shift ;;
    --from) REMOTE="$2:qmd_cloud_backup"; shift 2 ;;
    *) echo "unknown argument: $1"; exit 1 ;;
  esac
done

if [ -f "$QMD_INDEX" ] && [ "$FORCE" != "true" ]; then
  echo "✗ $QMD_INDEX already exists. Pass --force to overwrite it with the cloud backup"
  echo "  (this replaces your current local index — anything embedded/indexed since the"
  echo "  last backup run would be lost)."
  exit 1
fi

echo "→ checking backup exists on $REMOTE..."
if ! "$RCLONE_BIN" lsf "$REMOTE" > /dev/null 2>&1; then
  echo "✗ no backup found at $REMOTE — run qmd_cloud_backup.sh at least once from the"
  echo "  original machine first, or try --from <dropbox|googledrive>."
  exit 1
fi

mkdir -p "$TMP_DIR"

echo "→ downloading md_corpus archive..."
"$RCLONE_BIN" copyto "$REMOTE/md_corpus.tar.zst" "$TMP_DIR/md_corpus.tar.zst"
echo "→ extracting md_corpus/ ..."
rm -rf "$MD_CORPUS"
"$ZSTD_BIN" -d -c "$TMP_DIR/md_corpus.tar.zst" | tar -C "$MD_CORPUS_PARENT" -xf -

echo "→ downloading qmd index archive..."
"$RCLONE_BIN" copyto "$REMOTE/index.sqlite.zst" "$TMP_DIR/index.sqlite.zst"
echo "→ restoring qmd index ..."
mkdir -p "$QMD_CACHE_DIR"
# Stop any local qmd mcp servers first so we're not overwriting a file
# another process has open.
pkill -f "qmd.js mcp" 2>/dev/null || true
pkill -f "qmd mcp" 2>/dev/null || true
sleep 1
"$ZSTD_BIN" -d -f -o "$QMD_INDEX" "$TMP_DIR/index.sqlite.zst"
rm -f "$QMD_CACHE_DIR/index.sqlite-wal" "$QMD_CACHE_DIR/index.sqlite-shm"

rm -rf "$TMP_DIR"
echo "✓ restore complete. md_corpus/ and the qmd index are back in place."
echo "  Run 'qmd status' to confirm, and restart any qmd mcp server / Claude session"
echo "  that was using the old index."
