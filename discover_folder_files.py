#!/usr/bin/env python3
"""
Generalized version of discover_excel_files.py: add every file under one or
more specific top-level Dropbox/Google Drive folders to document_catalog.duckdb
that isn't already catalogued (checked by exact catalog_path match, not just
extension, so files already imported under a different path are correctly
left alone).

Why this exists: the original zotero_dropbox_import.py run left real gaps in
per-extension "junk drawer" folders (word/, pptx/, docx/, pdf/, ...) — e.g.
word/ (19 files) and pptx/ (25 files) had ZERO catalogued rows, and pdf/
(5,849 files) had only 254. This script is the bounded, per-folder version
of that fix — see discover_excel_files.py's own docstring for why an
unscoped account-wide rescan isn't practical (huge machine-backup trees
cause a 30min+ timeout).

Item keys are prefixed "XL_" — same non-Zotero-key convention as
discover_excel_files.py (not a coincidence: this and that script should
probably be merged if this pattern needs running again).

Usage:
  python3 discover_folder_files.py dropbox:word dropbox:pptx
  python3 discover_folder_files.py --dry-run dropbox:pdf
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
# Mirrors KNOWN_EXTENSIONS in pdf_to_markdown.py (what the converter can read).
DOC_EXTENSIONS = {"pdf", "docx", "pptx", "xlsx", "doc", "ppt", "xls", "epub", "mobi", "djvu", "chm", "ris", "html", "htm"}


def list_folder_files(remote, timeout=1800):
    cmd = [RCLONE_BIN, "lsf", remote, "-R", "--files-only", "--format", "pst", "--separator", "\t"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        print(f"  ✗ rclone lsf failed for {remote}: {result.stderr.strip()[-500:]}")
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
    import datetime
    try:
        dt = datetime.datetime.strptime(mtime_str[:19], "%Y-%m-%d %H:%M:%S")
        return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+", help="remote:folder targets, e.g. dropbox:word dropbox:pptx")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--docs-only", action="store_true",
                    help="only catalog convertible document types (pdf, docx, epub, ...), skipping images/video/installers")
    args = ap.parse_args()

    con = duckdb.connect(str(DB_PATH), read_only=args.dry_run)
    existing_paths = {r[0] for r in con.execute("select path from documents").fetchall()}
    print(f"catalog already has {len(existing_paths)} path(s)")

    all_new = []
    for target in args.targets:
        remote_name, _, folder = target.partition(":")
        remote_name = remote_name + ":"
        prefix = folder
        print(f"→ scanning {target} ...")
        rows = list_folder_files(target)
        print(f"  found {len(rows)} file(s) total under {target}")
        source = "dropbox" if remote_name == "dropbox:" else "googledrive"
        new_count = 0
        for rel_path, size_str, mtime_str in rows:
            full_path = f"{prefix}/{rel_path}" if prefix else rel_path
            if full_path in existing_paths:
                continue
            if args.docs_only and Path(rel_path).suffix.lstrip(".").lower() not in DOC_EXTENSIONS:
                continue
            new_count += 1
            all_new.append((source, full_path, size_str, mtime_str))
        print(f"  {new_count} not already catalogued")

    print(f"\ntotal new files across all targets: {len(all_new)}")
    if not all_new:
        con.close()
        return

    by_category = {}
    prepared = []
    for source, path, size_str, mtime_str in all_new:
        item_key = item_key_for(source, path)
        title = Path(path).name
        filetype = Path(path).suffix.lstrip(".").lower() or "unknown"
        topic, top_folder = extract_topic(path)
        category = classify_folder(top_folder)
        mtime_ms = mtime_ms_from_rclone(mtime_str)
        year = year_from_mtime_ms(mtime_ms)
        by_category[category] = by_category.get(category, 0) + 1
        prepared.append((item_key, title, path, source, category, topic, filetype,
                          str(year) if year else None, None, False, None, mtime_ms))

    print("by category:", by_category)
    by_filetype = {}
    for row in prepared:
        by_filetype[row[6]] = by_filetype.get(row[6], 0) + 1
    print("by filetype:", by_filetype)

    if args.dry_run:
        print("\n--dry-run: not writing to the database.")
        con.close()
        return

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
    print(f"\n✓ inserted {after - before} new row(s) into documents ({before} -> {after}).")


if __name__ == "__main__":
    main()
