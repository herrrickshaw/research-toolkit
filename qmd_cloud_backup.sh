#!/bin/bash
# Cloud backup for qmd's research corpus + its search index — compressed,
# single-archive uploads, not one small file at a time.
#
# Why a backup+restore pair instead of a live cloud-mounted index: qmd's
# index is an actively-queried SQLite database (several `qmd mcp` server
# processes hold it open on this Mac). SQLite's own docs warn against
# network filesystems for exactly this reason — many small random reads/
# writes, real corruption risk — and separately, this Mac has no macFUSE
# installed, so `rclone mount` isn't even available without the user
# personally approving a kernel extension. A daily backup + an on-demand
# restore script gets the real benefit (cloud-backed, portable to a new
# machine, survives local disk loss) without either risk.
#
# Why one compressed archive per side, not a folder sync: the first version
# of this script uploaded md_corpus/'s 42,214 individual .md files directly
# and hit Dropbox's rate limit mid-run ("too_many_write_operations",
# confirmed in this log's own history) — exactly the "many-small-files
# uploads catastrophically slowly against cloud APIs, tar it first" lesson
# already documented in cache_memory_cleaner.sh. Archiving first also
# shrinks the actual bytes transferred/stored a lot: this is plain text,
# which zstd compresses well.
#
# The index is snapshotted via `sqlite3 .backup` (a consistent copy, safe
# even while qmd mcp is running — not a raw copy of a possibly-mid-write
# file) and VACUUMed on that private snapshot copy (never on the live DB)
# before compression, reclaiming free-list pages (qmd status showed ~1%
# orphaned embedding chunks at last check).
#
# Usage:
#   ./qmd_cloud_backup.sh
set -euo pipefail

RCLONE_BIN=/opt/homebrew/bin/rclone
ZSTD_BIN=/opt/homebrew/bin/zstd
REMOTES=("dropbox:qmd_cloud_backup" "googledrive:qmd_cloud_backup")
MD_CORPUS=/Users/umashankar/research-toolkit/md_corpus
QMD_INDEX=/Users/umashankar/.cache/qmd/index.sqlite
SNAPSHOT_DIR=/Users/umashankar/research-toolkit/qmd_backup_snapshot
LOG=/Users/umashankar/research-toolkit/qmd_cloud_backup.log
ZSTD_LEVEL="${QMD_BACKUP_ZSTD_LEVEL:-15}"   # 1-22; 15 is a reasonable ratio/time balance for a nightly job

mkdir -p "$SNAPSHOT_DIR"
echo "=== qmd cloud backup started $(date) ===" >> "$LOG"

# --- md_corpus: tar + zstd into one archive -------------------------------
CORPUS_ARCHIVE="$SNAPSHOT_DIR/md_corpus.tar.zst"
echo "archiving md_corpus/ ($(du -sh "$MD_CORPUS" 2>/dev/null | cut -f1)) -> $CORPUS_ARCHIVE ..." >> "$LOG"
tar -C "$(dirname "$MD_CORPUS")" -cf - "$(basename "$MD_CORPUS")" \
  | "$ZSTD_BIN" -T0 -"$ZSTD_LEVEL" -o "$CORPUS_ARCHIVE" -f >> "$LOG" 2>&1
echo "md_corpus archive size: $(du -sh "$CORPUS_ARCHIVE" 2>/dev/null | cut -f1)" >> "$LOG"

# --- qmd index: sqlite3 .backup (consistent) -> VACUUM (on the copy, not
# the live DB) -> zstd -------------------------------------------------
INDEX_SNAPSHOT="$SNAPSHOT_DIR/index.sqlite"
INDEX_ARCHIVE="$SNAPSHOT_DIR/index.sqlite.zst"
echo "snapshotting qmd index via sqlite3 .backup (consistent copy, safe even while qmd mcp is running)..." >> "$LOG"
rm -f "$INDEX_SNAPSHOT" "$INDEX_ARCHIVE"
sqlite3 "$QMD_INDEX" ".backup '$INDEX_SNAPSHOT'" >> "$LOG" 2>&1
echo "raw snapshot size: $(du -sh "$INDEX_SNAPSHOT" 2>/dev/null | cut -f1)" >> "$LOG"
echo "VACUUMing the snapshot copy (never the live DB)..." >> "$LOG"
sqlite3 "$INDEX_SNAPSHOT" "VACUUM;" >> "$LOG" 2>&1
echo "vacuumed size: $(du -sh "$INDEX_SNAPSHOT" 2>/dev/null | cut -f1)" >> "$LOG"
echo "compressing index snapshot..." >> "$LOG"
"$ZSTD_BIN" -T0 -"$ZSTD_LEVEL" -o "$INDEX_ARCHIVE" -f "$INDEX_SNAPSHOT" >> "$LOG" 2>&1
echo "index archive size: $(du -sh "$INDEX_ARCHIVE" 2>/dev/null | cut -f1)" >> "$LOG"
rm -f "$INDEX_SNAPSHOT"

# --- upload the two archives to every configured remote -------------------
for remote in "${REMOTES[@]}"; do
  echo "uploading to $remote ..." >> "$LOG"
  "$RCLONE_BIN" copyto "$CORPUS_ARCHIVE" "$remote/md_corpus.tar.zst" >> "$LOG" 2>&1
  "$RCLONE_BIN" copyto "$INDEX_ARCHIVE" "$remote/index.sqlite.zst" >> "$LOG" 2>&1
done

rm -f "$CORPUS_ARCHIVE" "$INDEX_ARCHIVE"
echo "=== qmd cloud backup complete $(date) ===" >> "$LOG"
