#!/usr/bin/env python3
"""
Finds every archive file (.zip, .tar, .tar.gz, .tgz) across the whole
remote, extracts each one (one level deep - nested archives inside are
logged and skipped, not recursively expanded), and feeds any document-type
files found inside through the same Zotero import pipeline as everything
else (zotero_dropbox_import.process_file).

.rar / .7z are logged as unsupported (install unrar/7z to handle those)
rather than silently skipped.

Disk-space safe by construction: only one archive is downloaded+extracted
at a time, both size (compressed, before download) and total uncompressed
size (after download, before extracting) are checked against currently
free disk space with a safety margin, and everything for that archive
(the download + the extracted tree) is deleted before moving to the next
archive - so peak extra disk usage is bounded by roughly one archive's
uncompressed size, never the sum of all archives.

Resumable: archive-level progress lives in ARCHIVE_LOG_FILE (JSONL);
per-file progress reuses the shared zotero_import_log.jsonl via process_file.
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zotero_dropbox_import import (
    RCLONE_REMOTE, STAGING_DIR, ALLOWED_EXTENSIONS, EXCLUDE_TOP_FOLDERS,
    load_done, process_file, download, get_top_level_folders,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_LOG_FILE = os.environ.get("ZOTERO_ARCHIVE_LOG", os.path.join(HERE, "zotero_archive_log.jsonl"))
ARCHIVE_EXTENSIONS = {".zip", ".tar", ".gz", ".tgz"}
UNSUPPORTED_EXTENSIONS = {".rar", ".7z"}
SAFETY_MARGIN_BYTES = int(os.environ.get("ZOTERO_DISK_SAFETY_MARGIN_GB", "5")) * 1024**3


def free_bytes():
    return shutil.disk_usage(STAGING_DIR).free


def load_archive_done():
    done = set()
    if os.path.exists(ARCHIVE_LOG_FILE):
        with open(ARCHIVE_LOG_FILE) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("status") == "ok":
                    done.add(rec["path"])
    return done


def log_archive(rec):
    with open(ARCHIVE_LOG_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _rclone_lsjson(target, extra_args, timeout):
    try:
        proc = subprocess.run(["rclone", "lsjson", target] + extra_args,
                               capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        print(f"  ! list failed for {target!r}: {proc.stderr.strip()[:200]}", file=sys.stderr)
        return []
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []


ARCHIVE_INCLUDE = ["--include", "*.{zip,tar,gz,tgz,rar,7z}"]


def find_archives_in(folder, max_depth=5):
    """List archive files under one top-level folder (or "" for root loose
    files), recursively subdividing into subfolders on timeout (same
    strategy as zotero_dropbox_import.list_folder) instead of giving up on
    the whole folder - so huge trees (full machine/Drive backups) still get
    their archives found, just via smaller chunks."""
    return _find_archives_rec(folder, depth=0, max_depth=max_depth)


def _find_archives_rec(folder, depth, max_depth):
    target = f"{RCLONE_REMOTE}{folder}" if folder else RCLONE_REMOTE
    args = ARCHIVE_INCLUDE + (["--recursive"] if folder else [])
    entries = _rclone_lsjson(target, ["--files-only"] + args, timeout=300)
    if entries is not None:
        prefix = f"{folder}/" if folder else ""
        return [(f"{prefix}{e['Path']}", e.get("Size", 0)) for e in entries]

    if depth >= max_depth:
        print(f"  ! {folder!r}: still too large to list archives at max depth {max_depth} - skipped", file=sys.stderr)
        log_archive({"path": f"__folder__:{folder}", "status": "fail", "stage": "listing-timeout-max-depth", "time": time.time()})
        return []

    print(f"  ! {folder or '(root)'!r} timed out listing archives (depth {depth}) - subdividing", flush=True)
    log_archive({"path": f"__folder__:{folder}", "status": "fail", "stage": "listing-timeout-subdividing", "depth": depth, "time": time.time()})

    out = []
    direct = _rclone_lsjson(target, ["--files-only"] + ARCHIVE_INCLUDE, timeout=120)
    if direct:
        prefix = f"{folder}/" if folder else ""
        out.extend((f"{prefix}{e['Path']}", e.get("Size", 0)) for e in direct)

    subdirs = _rclone_lsjson(target, ["--dirs-only"], timeout=120)
    if not subdirs:
        return out
    for e in subdirs:
        child = f"{folder}/{e['Name']}" if folder else e["Name"]
        out.extend(_find_archives_rec(child, depth + 1, max_depth))
    return out


def extract_archive(local_path, dest_dir, ext):
    """Returns (ok, error_message, nested_archive_names)."""
    nested = []
    try:
        if ext == ".zip":
            with zipfile.ZipFile(local_path) as zf:
                total_uncompressed = sum(i.file_size for i in zf.infolist())
                if total_uncompressed > free_bytes() - SAFETY_MARGIN_BYTES:
                    return False, f"uncompressed size {total_uncompressed} exceeds free space", []
                zf.extractall(dest_dir)
                nested = [i.filename for i in zf.infolist() if os.path.splitext(i.filename)[1].lower() in ARCHIVE_EXTENSIONS | UNSUPPORTED_EXTENSIONS]
        elif ext in (".tar", ".gz", ".tgz"):
            mode = "r:gz" if ext in (".gz", ".tgz") else "r:"
            with tarfile.open(local_path, mode) as tf:
                total_uncompressed = sum(m.size for m in tf.getmembers() if m.isfile())
                if total_uncompressed > free_bytes() - SAFETY_MARGIN_BYTES:
                    return False, f"uncompressed size {total_uncompressed} exceeds free space", []
                tf.extractall(dest_dir, filter="data")
                nested = [m.name for m in tf.getmembers() if m.isfile() and os.path.splitext(m.name)[1].lower() in ARCHIVE_EXTENSIONS | UNSUPPORTED_EXTENSIONS]
        else:
            return False, f"unsupported extension {ext}", []
    except Exception as e:
        return False, str(e), []
    return True, None, nested


def process_one_archive(archive_path, size, doc_done, counters):
    ext = os.path.splitext(archive_path)[1].lower()
    if ext in UNSUPPORTED_EXTENSIONS:
        print(f"[UNSUPPORTED] {archive_path} ({ext}, no unrar/7z installed)", flush=True)
        log_archive({"path": archive_path, "status": "fail", "stage": "unsupported-format", "time": time.time()})
        return

    if size > free_bytes() - SAFETY_MARGIN_BYTES:
        print(f"[SKIP-TOO-LARGE] {archive_path} ({size / 1e9:.2f} GB, not enough free disk to even download)", flush=True)
        log_archive({"path": archive_path, "status": "fail", "stage": "too-large-compressed", "size": size, "time": time.time()})
        return

    print(f"\n=== Archive: {archive_path} ({size / 1e6:.1f} MB) ===", flush=True)
    local_archive = os.path.join(STAGING_DIR, f"archive_{uuid.uuid4().hex}{ext}")
    dl_ok, dl_err = download(archive_path, local_archive)
    if not dl_ok:
        print(f"  [DL-FAIL] {dl_err}", flush=True)
        log_archive({"path": archive_path, "status": "fail", "stage": "download", "error": dl_err, "time": time.time()})
        return

    extract_dir = os.path.join(STAGING_DIR, f"extract_{uuid.uuid4().hex}")
    os.makedirs(extract_dir, exist_ok=True)
    ex_ok, ex_err, nested = extract_archive(local_archive, extract_dir, ext)
    os.remove(local_archive)

    if not ex_ok:
        print(f"  [EXTRACT-FAIL] {ex_err}", flush=True)
        log_archive({"path": archive_path, "status": "fail", "stage": "extract", "error": ex_err, "time": time.time()})
        shutil.rmtree(extract_dir, ignore_errors=True)
        return

    if nested:
        print(f"  ! {len(nested)} nested archive(s) found inside, not expanded: {nested[:5]}", flush=True)

    found = 0
    for root, _, files in os.walk(extract_dir):
        for fname in files:
            fext = os.path.splitext(fname)[1].lower()
            if fext not in ALLOWED_EXTENSIONS:
                continue
            found += 1
            real_path = os.path.join(root, fname)
            rel_inside = os.path.relpath(real_path, extract_dir)
            virtual_path = f"{archive_path}!{rel_inside}"
            virtual_size = os.path.getsize(real_path)
            # process_file() re-downloads by dropbox_path via rclone, which
            # won't work for a path *inside* an archive - so inline the
            # same save logic directly on the already-extracted file.
            _save_extracted_file(real_path, virtual_path, virtual_size, doc_done, counters)

    shutil.rmtree(extract_dir, ignore_errors=True)
    print(f"  {found} document-type files found inside, processed", flush=True)
    log_archive({"path": archive_path, "status": "ok", "files_found": found, "nested_archives": nested, "time": time.time()})


def main():
    os.makedirs(STAGING_DIR, exist_ok=True)
    doc_done = load_done()
    counters = {"seen": 0, "ok": 0, "fail": 0, "skip": 0}

    folders = [""] + [f for f in get_top_level_folders() if f not in EXCLUDE_TOP_FOLDERS]
    print(f"Processing archives across {len(folders)} top-level locations, one folder at a time...", flush=True)

    for folder in folders:
        archive_done = load_archive_done()  # refresh - this run may have added to it
        found = find_archives_in(folder)  # subdivides internally on timeout, never raises
        pending = [(p, s) for p, s in found if p not in archive_done]
        if not pending:
            continue
        print(f"  {folder or '(root)'}: {len(pending)} archive(s) to process", flush=True)
        for archive_path, size in pending:
            process_one_archive(archive_path, size, doc_done, counters)

    print(f"\n=== ARCHIVES DONE: files seen={counters['seen']} ok={counters['ok']} fail={counters['fail']} skipped={counters['skip']} ===", flush=True)


def _save_extracted_file(real_path, virtual_path, size, done, counters):
    from zotero_dropbox_import import save_to_zotero, log, THROTTLE_SECONDS
    counters["seen"] += 1
    if virtual_path in done:
        counters["skip"] += 1
        return
    basename = os.path.basename(real_path)
    ok, status, body = save_to_zotero(real_path, basename, virtual_path)
    if ok:
        log({"path": virtual_path, "status": "ok", "size": size, "time": time.time()})
        counters["ok"] += 1
    else:
        log({"path": virtual_path, "status": "fail", "stage": "zotero", "http_status": status, "body": str(body)[:300], "time": time.time()})
        counters["fail"] += 1
        print(f"    [ZOTERO-FAIL] {virtual_path}: {status} {str(body)[:150]}", flush=True)
    time.sleep(THROTTLE_SECONDS)


if __name__ == "__main__":
    main()
