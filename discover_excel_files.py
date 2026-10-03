#!/usr/bin/env python3
"""
One-off discovery pass: find every .xlsx/.xls file on the dropbox:/
googledrive: remotes and add it to document_catalog.duckdb's `documents`
table, so pdf_to_markdown.py picks it up like any other catalogued
document. Not part of the regular pipeline — Excel was never in
zotero_dropbox_import.py's ALLOWED_EXTENSIONS, so nothing spreadsheet-
shaped exists in the catalog otherwise (confirmed: zero xlsx/xls rows
before this script runs).

Item keys for these rows are prefixed "XL_" (never a real Zotero item —
the rest of the catalog's 8-char keys come from actual Zotero items, and
this table has no other source of truth for "is this a real Zotero key").
Deterministic per (source, path), so rerunning is idempotent via
ON CONFLICT DO NOTHING.

Reuses categorize.py's existing category/topic classifier so these rows
get the same personal/research/backup tagging as everything else in the
catalog (the user explicitly asked for all Excel files including
personal/financial ones — this still tags them correctly for anyone
querying the catalog later).

Usage:
  python3 discover_excel_files.py            # scan + insert
  python3 discover_excel_files.py --dry-run  # scan + report counts, no DB write
"""
import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, "/Users/umashankar/research-toolkit")
from categorize import classify_folder, extract_topic, year_from_mtime_ms

REPO_DIR = Path("/Users/umashankar/research-toolkit")
DB_PATH = REPO_DIR / "document_catalog.duckdb"
RCLONE_BIN = "/opt/homebrew/bin/rclone"
EXTENSIONS = ("*.xlsx", "*.xls")

# A full unscoped `rclone lsf -R` timed out at 30min without finishing — this
# Dropbox account has huge machine-backup trees mixed in (AVG AntiVirus
# files, laptop_backup, My Mac (...), Machine_Backup_2026-09, etc., likely
# millions of files) that make a flat account-wide scan impractical.
# Dropbox already has dedicated per-extension top-level folders matching
# exactly what the original import scanned (pdf/, docx/, pptx/, epub/,
# mobi/, djvu/, chm/, ris/, html/ all exist) — "xlsx" and "excel" are the
# same pattern, just never included in that import's ALLOWED_EXTENSIONS.
# Scoping to those two folders is fast and high-confidence; Google Drive
# has no equivalent dedicated folder, so it's scanned in full (its account
# is ~60k files, not millions - tractable, unlike Dropbox's backup trees).
SCAN_TARGETS = [
    ("dropbox", "dropbox:xlsx"),
    ("dropbox", "dropbox:excel"),
    ("googledrive", "googledrive:"),
]


def list_excel_files(source, remote, timeout=1800):
    cmd = [RCLONE_BIN, "lsf", remote, "-R", "--files-only", "--format", "pst", "--separator", "\t"]
    for ext in EXTENSIONS:
        cmd += ["--include", ext]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        print(f"  ✗ rclone lsf failed for {source}: {result.stderr.strip()[-500:]}")
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        path, size_str, mtime_str = parts
        rows.append((path, size_str, mtime_str))
    return rows


def item_key_for(source, path):
    h = hashlib.md5(f"{source}:{path}".encode()).hexdigest()[:12]
    return f"XL_{h}"


def mtime_ms_from_rclone(mtime_str):
    # rclone's default lsf time format: "2006-01-02 15:04:05"
    import datetime
    try:
        dt = datetime.datetime.strptime(mtime_str[:19], "%Y-%m-%d %H:%M:%S")
        return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    all_rows = []
    for source, remote in SCAN_TARGETS:
        print(f"→ scanning {remote} for .xlsx/.xls ...")
        rows = list_excel_files(source, remote)
        print(f"  found {len(rows)} file(s) under {remote}")
        # rclone lsf returns paths relative to the scanned remote root — for
        # a scoped target like "dropbox:xlsx", prepend that folder back so
        # catalog_path stays root-relative, matching every other row
        # (pdf_to_markdown.py fetches via f"{remote_root}{catalog_path}").
        prefix = remote.split(":", 1)[1] if ":" in remote else ""
        remote_root = remote.split(":", 1)[0] + ":"
        for path, size_str, mtime_str in rows:
            full_path = f"{prefix}/{path}" if prefix else path
            all_rows.append((source, full_path, size_str, mtime_str))

    print(f"\ntotal Excel files found: {len(all_rows)}")
    if not all_rows:
        return

    by_category = {}
    prepared = []
    for source, path, size_str, mtime_str in all_rows:
        item_key = item_key_for(source, path)
        title = Path(path).name
        filetype = Path(path).suffix.lstrip(".").lower() or "xlsx"
        topic, top_folder = extract_topic(path)
        category = classify_folder(top_folder)
        mtime_ms = mtime_ms_from_rclone(mtime_str)
        year = year_from_mtime_ms(mtime_ms)
        by_category[category] = by_category.get(category, 0) + 1
        prepared.append((item_key, title, path, source, category, topic, filetype,
                          str(year) if year else None, None, False, None, mtime_ms))

    print("by category:", by_category)

    if args.dry_run:
        print("\n--dry-run: not writing to the database. Sample rows:")
        for row in prepared[:5]:
            print(" ", row)
        return

    con = duckdb.connect(str(DB_PATH))
    before = con.execute("select count(*) from documents").fetchone()[0]
    con.executemany("""
        insert into documents
            (item_key, title, path, source, category, topic, filetype, year,
             md5, is_duplicate, duplicate_of, mtime, date_added, auto_topic)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp, NULL)
        on conflict (item_key) do nothing
    """, prepared)
    after = con.execute("select count(*) from documents").fetchone()[0]
    con.close()
    print(f"\n✓ inserted {after - before} new row(s) into documents "
          f"({before} -> {after}; {len(prepared) - (after - before)} already present, skipped).")


if __name__ == "__main__":
    main()
