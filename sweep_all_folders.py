#!/usr/bin/env python3
"""
Account-wide discovery sweep: find document-type files anywhere on
dropbox:/googledrive: that aren't already in document_catalog.duckdb's
`documents` table, including inside backup trees, and add them.

Why not one big `rclone lsf -R`: already tried (discover_excel_files.py's
first attempt) and it timed out at 30 minutes without finishing — this
Dropbox account has huge machine-backup folders (My Mac (...), laptop_backup,
Machine_Backup_2026-09, AVG AntiVirus files, ...) that make a flat scan of
the whole account impractical.

Methodology (same "recursive subdivision" the README already documents for
this exact problem): try a bounded, timed `rclone lsf -R` on a folder; if it
times out, list just that folder's direct subfolders (fast) and recurse
into each independently with a lower depth budget, going deeper only where
needed. A branch that still can't be listed at max depth is logged and
skipped rather than blocking the whole sweep.

Zip/tar files are inventoried (counted, logged to
sweep_archives_found.jsonl) but NOT auto-extracted — that's a separate,
heavier, disk-intensive decision (see process_archives.py, which writes
into the live Zotero app, not this catalog, and would need adapting).

Item keys are prefixed "XL_" (same non-Zotero-key convention as
discover_excel_files.py / discover_folder_files.py).

Usage:
  python3 sweep_all_folders.py --dry-run          # report only, no DB writes
  python3 sweep_all_folders.py                    # scan + insert new files
  python3 sweep_all_folders.py --max-depth 5       # deeper subdivision (default 4)
  python3 sweep_all_folders.py --per-call-timeout 120  # seconds (default 180)
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, "/Users/umashankar/research-toolkit")
from categorize import classify_folder, extract_topic, year_from_mtime_ms

REPO_DIR = Path("/Users/umashankar/research-toolkit")
DB_PATH = REPO_DIR / "document_catalog.duckdb"
RCLONE_BIN = "/opt/homebrew/bin/rclone"
SWEEP_LOG = REPO_DIR / "sweep_run.log"
ARCHIVES_LOG = REPO_DIR / "sweep_archives_found.jsonl"
SKIPPED_LOG = REPO_DIR / "sweep_skipped_folders.jsonl"
CHECKPOINT_FILE = REPO_DIR / "sweep_checkpoint.jsonl"

DOCUMENT_EXTENSIONS = {"pdf", "docx", "pptx", "doc", "ppt", "epub", "mobi",
                        "djvu", "chm", "ris", "html", "htm", "xlsx", "xls"}
ARCHIVE_EXTENSIONS = {"zip", "tar", "gz", "tgz"}

# Already fully swept in earlier passes — skip re-walking these top-level
# buckets (their content is already in the catalog or was already handled).
ALREADY_COVERED_TOP_FOLDERS = {"xlsx", "excel", "word", "pptx", "docx", "pdf"}

# Zotero's OWN per-attachment storage: one subfolder per item, tens of
# thousands of them, each near-empty (0-2 files) — confirmed by watching it
# grind at ~1s/subfolder with no end in sight. This is Zotero's storage
# mirror of content the account's Zotero import already processed, not new
# material, so the time cost (many hours) isn't worth it here. Revisit with
# a different strategy (e.g. reading Zotero's own item index instead of
# listing every storage folder) if this content is specifically wanted.
PATHOLOGICAL_SKIP_FOLDERS = {
    "zotero", "zotero_library_organized", "zotero_migrated_catalog",
    "zotero_storage_remainder", "zotero_unsorted_bulk_files",
}

REMOTES = ["dropbox:", "googledrive:"]


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(SWEEP_LOG, "a") as f:
        f.write(line + "\n")


def rclone_lsd(remote, timeout=60):
    """Direct subfolders only (fast, non-recursive). Returns None on
    failure OR timeout — a folder whose *subfolder listing* itself times
    out (seen on dropbox:Zotero, which has tens of thousands of small
    per-item storage folders) is treated as unlistable, same as a failed
    recursive scan, rather than crashing the whole sweep."""
    try:
        result = subprocess.run(
            [RCLONE_BIN, "lsf", remote, "--dirs-only"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return [d.rstrip("/") for d in result.stdout.splitlines() if d.strip()]


def rclone_lsf_recursive(remote, timeout):
    """Full recursive file listing of one folder. Returns None on timeout
    (caller subdivides) rather than raising."""
    try:
        result = subprocess.run(
            [RCLONE_BIN, "lsf", remote, "-R", "--files-only", "--format", "pst", "--separator", "\t"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append(tuple(parts))
    return rows


def walk(remote_root, folder_path, depth, max_depth, per_call_timeout, found, archives, skipped):
    """folder_path is relative to remote_root ('' = root itself)."""
    target = f"{remote_root}{folder_path}" if folder_path else remote_root
    rows = rclone_lsf_recursive(target, per_call_timeout)
    if rows is not None:
        for rel_path, size_str, mtime_str in rows:
            full_path = f"{folder_path}/{rel_path}" if folder_path else rel_path
            ext = Path(full_path).suffix.lstrip(".").lower()
            if ext in DOCUMENT_EXTENSIONS:
                found.append((full_path, size_str, mtime_str))
            elif ext in ARCHIVE_EXTENSIONS:
                archives.append({"path": full_path, "size": size_str, "mtime": mtime_str})
        log(f"  ✓ {target} — {len(rows)} file(s) listed (depth {depth})")
        return

    if depth >= max_depth:
        skipped.append({"path": target, "reason": f"timed out at max depth {max_depth}"})
        log(f"  ✗ {target} — timed out, at max depth, SKIPPED")
        return

    log(f"  … {target} timed out at depth {depth}, subdividing")
    subfolders = rclone_lsd(target, timeout=60)
    if subfolders is None:
        skipped.append({"path": target, "reason": "lsd (subfolder listing) itself failed"})
        log(f"  ✗ {target} — even subfolder listing failed, SKIPPED")
        return
    if not subfolders:
        # Times out with -R but has no subfolders? Loose files at this
        # level only — retry once with a longer timeout, not recursively.
        rows = rclone_lsf_recursive(target, per_call_timeout * 3)
        if rows is not None:
            for rel_path, size_str, mtime_str in rows:
                full_path = f"{folder_path}/{rel_path}" if folder_path else rel_path
                ext = Path(full_path).suffix.lstrip(".").lower()
                if ext in DOCUMENT_EXTENSIONS:
                    found.append((full_path, size_str, mtime_str))
                elif ext in ARCHIVE_EXTENSIONS:
                    archives.append({"path": full_path, "size": size_str, "mtime": mtime_str})
            log(f"  ✓ {target} — {len(rows)} file(s) listed on retry")
        else:
            skipped.append({"path": target, "reason": "no subfolders but still times out"})
            log(f"  ✗ {target} — no subfolders, still times out, SKIPPED")
        return

    for sub in subfolders:
        sub_path = f"{folder_path}/{sub}" if folder_path else sub
        walk(remote_root, sub_path, depth + 1, max_depth, per_call_timeout, found, archives, skipped)


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


def connect_with_retry(read_only, max_wait_s=4 * 3600, poll_s=30):
    """document_catalog.duckdb is single-writer; pdf_to_markdown.py holds
    the lock for the whole duration of a batch (often 15-20+ min). The scan
    phase below doesn't touch the DB at all, so this is only called once
    at the very end — but that end might still land mid-batch, so retry
    with a generous ceiling rather than failing the whole multi-hour sweep
    over a transient lock."""
    deadline = time.time() + max_wait_s
    attempt = 0
    while True:
        try:
            return duckdb.connect(str(DB_PATH), read_only=read_only)
        except duckdb.IOException as e:
            attempt += 1
            if time.time() > deadline:
                raise
            log(f"  DB locked (attempt {attempt}), waiting {poll_s}s: {str(e)[:150]}")
            time.sleep(poll_s)


def load_checkpoint():
    """{(remote, folder): True} for every top-level unit already swept in a
    prior (possibly crashed) run, plus the accumulated results so far —
    resuming never redoes completed work, and a future crash only loses
    whatever top-level folder was in flight."""
    done = set()
    all_found, all_archives, all_skipped = [], [], []
    if not CHECKPOINT_FILE.exists():
        return done, all_found, all_archives, all_skipped
    with open(CHECKPOINT_FILE) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            done.add((rec["remote"], rec["folder"]))
            for path, sz, mt in rec.get("found", []):
                all_found.append((rec["source"], path, sz, mt))
            all_archives.extend(rec.get("archives", []))
            all_skipped.extend(rec.get("skipped", []))
    return done, all_found, all_archives, all_skipped


def checkpoint(remote, folder, source, found, archives, skipped):
    with open(CHECKPOINT_FILE, "a") as f:
        f.write(json.dumps({
            "remote": remote, "folder": folder, "source": source,
            "found": found, "archives": archives, "skipped": skipped,
        }) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-depth", type=int, default=4)
    ap.add_argument("--per-call-timeout", type=int, default=180)
    ap.add_argument("--fresh", action="store_true", help="ignore any existing checkpoint, start over")
    args = ap.parse_args()

    if args.fresh:
        CHECKPOINT_FILE.unlink(missing_ok=True)
    done_units, all_found, all_archives, all_skipped = load_checkpoint()
    if done_units:
        log(f"resuming from checkpoint: {len(done_units)} unit(s) already swept, "
            f"{len(all_found)} document(s)/{len(all_archives)} archive(s) carried forward")

    for remote in REMOTES:
        source = "dropbox" if remote == "dropbox:" else "googledrive"
        log(f"=== sweeping {remote} ===")
        top_folders = rclone_lsd(remote, timeout=60) or []
        log(f"{len(top_folders)} top-level folder(s) on {remote}")

        # Root-level loose files first (not in any folder) — non-recursive,
        # cheap regardless of account size. Checkpointed as folder "".
        if (remote, "") not in done_units:
            found, archives = [], []
            try:
                result = subprocess.run(
                    [RCLONE_BIN, "lsf", remote, "--files-only", "--format", "pst", "--separator", "\t"],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        parts = line.split("\t")
                        if len(parts) == 3:
                            path, size_str, mtime_str = parts
                            ext = Path(path).suffix.lstrip(".").lower()
                            if ext in DOCUMENT_EXTENSIONS:
                                found.append((path, size_str, mtime_str))
                                all_found.append((source, path, size_str, mtime_str))
                            elif ext in ARCHIVE_EXTENSIONS:
                                archives.append({"path": path})
                                all_archives.append({"source": source, "path": path})
            except subprocess.TimeoutExpired:
                pass
            checkpoint(remote, "", source, found, archives, [])

        for folder in top_folders:
            if remote == "dropbox:" and folder.lower() in ALREADY_COVERED_TOP_FOLDERS:
                continue
            if folder.lower() in PATHOLOGICAL_SKIP_FOLDERS:
                log(f"skip {remote}{folder} (pathologically slow — Zotero's own per-item storage mirror)")
                checkpoint(remote, folder, source, [], [], [{"path": f"{remote}{folder}", "reason": "pathological_skip"}])
                continue
            if (remote, folder) in done_units:
                continue
            found, archives, skipped = [], [], []
            walk(remote, folder, 0, args.max_depth, args.per_call_timeout, found, archives, skipped)
            for path, size_str, mtime_str in found:
                all_found.append((source, path, size_str, mtime_str))
            for a in archives:
                a["source"] = source
                all_archives.append(a)
            all_skipped.extend(skipped)
            checkpoint(remote, folder, source, found, archives, skipped)

    log(f"\nscan complete: {len(all_found)} document file(s) found across both remotes, "
        f"{len(all_archives)} archive file(s), {len(all_skipped)} folder(s) skipped (too big even "
        f"after subdivision).")

    with open(ARCHIVES_LOG, "w") as f:
        for a in all_archives:
            f.write(json.dumps(a) + "\n")
    with open(SKIPPED_LOG, "w") as f:
        for s in all_skipped:
            f.write(json.dumps(s) + "\n")

    # Only now do we need the database — connect (retrying through any
    # in-progress pdf_to_markdown.py batch) to filter out what's already
    # catalogued and, unless --dry-run, insert the rest.
    log("opening document_catalog.duckdb for dedup + insert...")
    con = connect_with_retry(read_only=args.dry_run)
    existing_paths = {r[0] for r in con.execute("select path from documents").fetchall()}
    log(f"catalog has {len(existing_paths)} path(s)")

    new_rows = [(s, p, sz, mt) for s, p, sz, mt in all_found if p not in existing_paths]
    log(f"new (not already catalogued): {len(new_rows)}")

    if not new_rows:
        con.close()
        return

    by_category, by_filetype = {}, {}
    prepared = []
    for source, path, size_str, mtime_str in new_rows:
        item_key = item_key_for(source, path)
        title = Path(path).name
        filetype = Path(path).suffix.lstrip(".").lower() or "unknown"
        topic, top_folder = extract_topic(path)
        category = classify_folder(top_folder)
        mtime_ms = mtime_ms_from_rclone(mtime_str)
        year = year_from_mtime_ms(mtime_ms)
        by_category[category] = by_category.get(category, 0) + 1
        by_filetype[filetype] = by_filetype.get(filetype, 0) + 1
        prepared.append((item_key, title, path, source, category, topic, filetype,
                          str(year) if year else None, None, False, None, mtime_ms))

    log(f"by category: {by_category}")
    log(f"by filetype: {by_filetype}")

    if args.dry_run:
        log("--dry-run: not writing to the database.")
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
    log(f"✓ inserted {after - before} new row(s) into documents ({before} -> {after}).")


if __name__ == "__main__":
    main()
