#!/usr/bin/env python3
"""
Copies (never moves) every catalogued document into a clean, category/topic
organized folder tree within the SAME cloud remote it came from - originals
are left completely untouched at their current locations.

  dropbox:Organized/<category>/<topic>/<basename>
  googledrive:Organized/<category>/<topic>/<basename>

Uses `rclone copyto` for server-side copies (same remote, no download+reupload
through this machine - fast and doesn't touch local disk or bandwidth).
Driven entirely by the DuckDB catalog, not Zotero's API - this can run at
full speed regardless of whether Zotero's local API is healthy.

Filename collisions (two different source files sharing a basename, common
given how many duplicate/near-duplicate copies exist across backup folders)
are resolved by prefixing the destination with the item's own Zotero key -
short, stable, and guaranteed unique.

Only items with category/topic already known are copied on a given pass;
items still lacking tags (because backfill hasn't reached them yet) are
skipped and will be picked up on a later re-run once retagged - re-run this
script any time after refreshing the DuckDB catalog to top up newly-tagged
items. Idempotent: a destination that already exists (per COPY_LOG_FILE) is
skipped, so re-running is cheap.
"""
import json
import os
import re
import subprocess
import sys

DB_PATH = "/Users/umashankar/research-toolkit/document_catalog.duckdb"
COPY_LOG_FILE = "/Users/umashankar/research-toolkit/cloud_copy_log.jsonl"

REMOTE_MAP = {
    "dropbox": "dropbox:",
    "googledrive": "googledrive:",
}


def safe_component(name):
    """Folder/file name component safe for all of Dropbox/Google Drive/local fs."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return name[:150] if name else "misc"


def already_copied(log_path):
    done = set()
    if os.path.exists(COPY_LOG_FILE):
        with open(COPY_LOG_FILE) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("status") == "ok":
                        done.add(rec["item_key"])
                except json.JSONDecodeError:
                    continue
    return done


def log_action(rec):
    with open(COPY_LOG_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


def main():
    import duckdb
    con = duckdb.connect(DB_PATH, read_only=True)

    rows = con.execute("""
        SELECT item_key, title, path, source, category, topic
        FROM documents
        WHERE source IN ('dropbox', 'googledrive')
          AND category IS NOT NULL
          AND is_duplicate = false
    """).fetchall()
    print(f"{len(rows)} non-duplicate, categorized documents eligible to copy", flush=True)

    done = already_copied(COPY_LOG_FILE)
    print(f"{len(done)} already copied in a prior run - skipping those", flush=True)

    copied = skipped = failed = archive_skipped = 0
    for item_key, title, path, source, category, topic in rows:
        if item_key in done:
            skipped += 1
            continue

        remote = REMOTE_MAP.get(source)
        if remote is None:
            skipped += 1
            continue

        if path and "!" in path:
            # Archive-extracted virtual path (zip/x.zip!inner/file.pdf) - not
            # a real cloud object, rclone can't fetch it directly. Needs a
            # download-parent-zip + extract-inner-file + upload pass instead,
            # not built yet. Skip cleanly rather than attempt a doomed copy.
            archive_skipped += 1
            log_action({"item_key": item_key, "path": path, "status": "skip-archive-virtual-path"})
            continue

        basename = os.path.basename(path.split("!")[-1]) if path else (title or item_key)
        dest_name = f"{item_key}_{safe_component(basename)}"
        dest_path = f"{remote}Organized/{safe_component(category)}/{safe_component(topic or 'misc')}/{dest_name}"
        src_path = f"{remote}{path}"

        try:
            result = subprocess.run(
                ["rclone", "copyto", src_path, dest_path, "--retries", "3", "--low-level-retries", "5"],
                capture_output=True, text=True, timeout=180,
            )
            ok = result.returncode == 0
            error = result.stderr[:300] if not ok else None
        except subprocess.TimeoutExpired:
            ok = False
            error = "rclone hung past 180s timeout"

        if ok:
            copied += 1
            log_action({"item_key": item_key, "path": path, "dest": dest_path, "status": "ok"})
            if copied % 100 == 0:
                print(f"  ... {copied} copied so far", flush=True)
        else:
            failed += 1
            log_action({"item_key": item_key, "path": path, "dest": dest_path, "status": "fail", "error": error})
            print(f"[FAIL] {path}: {error}", flush=True)

    print(f"\n=== CLOUD COPY DONE: copied={copied} skipped={skipped} archive_skipped={archive_skipped} failed={failed} (of {len(rows)} eligible) ===", flush=True)


if __name__ == "__main__":
    main()
