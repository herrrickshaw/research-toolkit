#!/usr/bin/env python3
"""
Streams documents from Dropbox (via rclone) into a locally running Zotero
app through its local CRUD API (zotero_crud.py) - not the connector's
/connector/saveStandaloneAttachment, which turned unreliable (bare 500s)
after a few hundred calls in one session.

Setup:
  1. Configure an rclone remote for Dropbox (`rclone config`).
  2. In Zotero: Settings > Advanced > Config Editor > set
     extensions.zotero.httpServer.localAPI.enabled = true.
  3. Create a Zotero collection to import into, select it, find its key via
     GET http://127.0.0.1:23119/api/users/0/collections, and export it as
     ZOTERO_COLLECTION_KEY (optional - omit to import into My Library root).
  4. export RCLONE_REMOTE=dropbox:   (or your remote's name)

Each file is downloaded to a scratch dir, created as a standalone attachment
item (title = filename, url = original dropbox:// path for provenance),
uploaded, then deleted locally. No automatic PDF/EPUB metadata recognition
here (that was a connector-only feature) - use Zotero's own "Retrieve
Metadata for PDF" in bulk afterward, or remediate_failed.py's internet-
lookup path for stragglers.

Resumable: successes/failures are appended to LOG_FILE (JSONL); a re-run
skips paths already marked "ok".
"""
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zotero_crud import ZoteroCRUD

HERE = os.path.dirname(os.path.abspath(__file__))
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "dropbox:")
DROPBOX_IMPORT_COLLECTION_KEY = os.environ.get("ZOTERO_COLLECTION_KEY", "")
STAGING_DIR = os.environ.get("ZOTERO_STAGING_DIR", os.path.join(HERE, "stage"))
LOG_FILE = os.environ.get("ZOTERO_IMPORT_LOG", os.path.join(HERE, "zotero_import_log.jsonl"))
THROTTLE_SECONDS = 0.3

_crud = None


def get_crud():
    global _crud
    if _crud is None:
        _crud = ZoteroCRUD()
    return _crud

ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".epub",
    ".djvu", ".mobi", ".chm", ".ris",
}

MIME_OVERRIDES = {
    ".djvu": "image/vnd.djvu",
    ".mobi": "application/x-mobipocket-ebook",
    ".chm": "application/vnd.ms-htmlhelp",
    ".ris": "application/x-research-info-systems",
}

# Full-account scope by default: every top-level folder except any you list
# here (comma-separated in EXCLUDE_TOP_FOLDERS env var). Always exclude your
# Zotero data directory itself if it happens to live inside the same remote -
# touching zotero.sqlite-wal while the app has it open would be actively
# dangerous, not just noisy.
EXCLUDE_TOP_FOLDERS = set(filter(None, os.environ.get("EXCLUDE_TOP_FOLDERS", "Zotero").split(",")))


def get_top_level_folders():
    proc = subprocess.run(
        ["rclone", "lsjson", RCLONE_REMOTE, "--dirs-only"],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        print(f"  ! failed to list top-level folders: {proc.stderr.strip()[:300]}", file=sys.stderr)
        return []
    entries = json.loads(proc.stdout or "[]")
    return [e["Name"] for e in entries if e["Name"] not in EXCLUDE_TOP_FOLDERS]


def load_done():
    done = set()
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("status") == "ok":
                    done.add(rec["path"])
    return done


def log(rec):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _rclone_lsjson(target, extra_args, timeout):
    try:
        proc = subprocess.run(
            ["rclone", "lsjson", target] + extra_args,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        print(f"  ! list failed for {target!r}: {proc.stderr.strip()[:200]}", file=sys.stderr)
        return []
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []


def list_folder(folder, max_depth=5):
    """Recursively subdivides on timeout: huge trees (full machine/Drive
    backups) can time out on a single --recursive listing even before any
    filtering, so on timeout this falls back to listing just the folder's
    direct subfolders (fast) and recurses into each independently - going
    deeper only where needed, capped at max_depth so it can't spin forever."""
    return _list_folder_rec(folder, depth=0, max_depth=max_depth)


def _list_folder_rec(folder, depth, max_depth):
    entries = _rclone_lsjson(f"{RCLONE_REMOTE}{folder}", ["--recursive", "--files-only"], timeout=300)
    if entries is not None:
        out = []
        for e in entries:
            rel = e["Path"]
            ext = os.path.splitext(rel)[1].lower()
            if ext in ALLOWED_EXTENSIONS:
                out.append((f"{folder}/{rel}", e.get("Size", 0)))
        return out

    # Timed out - subdivide.
    if depth >= max_depth:
        print(f"  ! {folder!r}: still too large to list at max depth {max_depth} - logged and skipped", file=sys.stderr)
        log({"path": f"__folder__:{folder}", "status": "fail", "stage": "listing-timeout-max-depth", "time": time.time()})
        return []

    print(f"  ! {folder!r} timed out listing (depth {depth}) - subdividing into subfolders", flush=True)
    log({"path": f"__folder__:{folder}", "status": "fail", "stage": "listing-timeout-subdividing", "depth": depth, "time": time.time()})

    out = []
    # Loose files directly in this folder (non-recursive, so bounded/fast).
    direct_files = _rclone_lsjson(f"{RCLONE_REMOTE}{folder}", ["--files-only"], timeout=120)
    if direct_files:
        for e in direct_files:
            ext = os.path.splitext(e["Path"])[1].lower()
            if ext in ALLOWED_EXTENSIONS:
                out.append((f"{folder}/{e['Path']}", e.get("Size", 0)))

    subdirs = _rclone_lsjson(f"{RCLONE_REMOTE}{folder}", ["--dirs-only"], timeout=120)
    if not subdirs:
        return out
    for e in subdirs:
        out.extend(_list_folder_rec(f"{folder}/{e['Name']}", depth + 1, max_depth))
    return out


def download(dropbox_path, local_path):
    proc = subprocess.run(
        ["rclone", "copyto", f"{RCLONE_REMOTE}{dropbox_path}", local_path],
        capture_output=True, text=True, timeout=1800,
    )
    return proc.returncode == 0, proc.stderr.strip()[:300]


def save_to_zotero(local_path, title, dropbox_path, retries=2, timeout=300):
    """Creates a standalone attachment item via Zotero's CRUD API and
    uploads the file. Replaces the connector's /saveStandaloneAttachment,
    which started returning bare 500s after a few hundred calls (session-
    state issue inside Zotero, not fixable from here) - the CRUD path has
    been reliable under the same load."""
    ext = os.path.splitext(local_path)[1].lower()
    content_type = MIME_OVERRIDES.get(ext) or mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    zc = get_crud()

    with open(local_path, "rb") as f:
        content = f.read()
    md5 = hashlib.md5(content).hexdigest()
    filesize = len(content)
    mtime_ms = int(os.path.getmtime(local_path) * 1000)
    basename = os.path.basename(local_path)

    item = {
        "itemType": "attachment",
        "linkMode": "imported_file",
        "title": title,
        "filename": basename,
        "contentType": content_type,
        "md5": md5,
        "mtime": mtime_ms,
        "url": f"dropbox://{dropbox_path}",
        "tags": [],
    }
    if DROPBOX_IMPORT_COLLECTION_KEY:
        item["collections"] = [DROPBOX_IMPORT_COLLECTION_KEY]

    status, resp = zc.create_items([item])
    item_key = resp.get("success", {}).get("0") if status == 200 else None
    if not item_key:
        return False, status, json.dumps(resp.get("failed", resp))[:300]

    ok, up_status, up_body = zc.upload_attachment_file(
        item_key, local_path, basename, content_type, md5, mtime_ms, filesize,
        retries=retries, timeout=timeout,
    )
    if ok:
        return True, up_status, up_body
    return False, up_status, str(up_body)[:300]


def process_file(dropbox_path, size, done, counters):
    """Download one file from Dropbox and save it to Zotero. Shared by the
    main folder scan and the archive-extraction pass (process_archives.py)."""
    counters["seen"] += 1
    if dropbox_path in done:
        counters["skip"] += 1
        return

    basename = os.path.basename(dropbox_path)
    local_path = os.path.join(STAGING_DIR, f"{uuid.uuid4().hex}_{basename}")

    ok, err = download(dropbox_path, local_path)
    if not ok:
        log({"path": dropbox_path, "status": "fail", "stage": "download", "error": err, "time": time.time()})
        counters["fail"] += 1
        print(f"  [DL-FAIL] {dropbox_path}: {err}", flush=True)
        return

    ok, status, body = save_to_zotero(local_path, basename, dropbox_path)
    try:
        os.remove(local_path)
    except OSError:
        pass

    if ok:
        log({"path": dropbox_path, "status": "ok", "size": size, "time": time.time()})
        counters["ok"] += 1
        if counters["ok"] % 25 == 0:
            print(f"  ... {counters['ok']} saved so far (last: {basename})", flush=True)
    else:
        log({"path": dropbox_path, "status": "fail", "stage": "zotero", "http_status": status, "body": body[:300], "time": time.time()})
        counters["fail"] += 1
        print(f"  [ZOTERO-FAIL] {dropbox_path}: {status} {body[:200]}", flush=True)

    time.sleep(THROTTLE_SECONDS)


def main():
    os.makedirs(STAGING_DIR, exist_ok=True)
    done = load_done()
    print(f"Already done: {len(done)} files", flush=True)
    counters = {"seen": 0, "ok": 0, "fail": 0, "skip": 0}

    folders = [""] + get_top_level_folders()  # "" = loose files at remote root
    print(f"Scanning {len(folders)} top-level locations (whole remote, minus {EXCLUDE_TOP_FOLDERS})", flush=True)

    for folder in folders:
        label = folder or "(root loose files)"
        print(f"\n=== Scanning: {label} ===", flush=True)
        files = list_folder(folder) if folder else list_root_files()
        print(f"  {len(files)} matching files", flush=True)
        for dropbox_path, size in files:
            process_file(dropbox_path, size, done, counters)

    print(f"\n=== DONE: seen={counters['seen']} ok={counters['ok']} fail={counters['fail']} skipped(already done)={counters['skip']} ===", flush=True)


def list_root_files():
    proc = subprocess.run(
        ["rclone", "lsjson", RCLONE_REMOTE, "--files-only"],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        return []
    entries = json.loads(proc.stdout or "[]")
    out = []
    for e in entries:
        ext = os.path.splitext(e["Path"])[1].lower()
        if ext in ALLOWED_EXTENSIONS:
            out.append((e["Path"], e.get("Size", 0)))
    return out


if __name__ == "__main__":
    main()
