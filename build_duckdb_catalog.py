#!/usr/bin/env python3
"""
DIY alternative to a reference-manager app: a flat DuckDB table describing
every document we've imported, built from Zotero's own data (which already
carries the category/topic/source/dedup tagging work done this session) -
not from re-scanning Dropbox/Google Drive again.

No app, no local HTTP server, no freeze risk: just a .duckdb file, queryable
with plain SQL. Full-text search is added via DuckDB's FTS extension over
title + path, which covers most of what a "search my archive" need actually
requires for content that's 90% non-academic.

Schema, one row per Zotero item with a url (i.e. every item our pipeline
created):
  item_key        Zotero item key (for cross-reference back, e.g. via
                   zotero-mcp or the local API, while Zotero is still in use)
  title           display title
  path            the real file location, e.g. "Documents/Pdf/foo.pdf" or,
                   for archive-extracted files, "zip/x.zip!inner/foo.pdf"
  source          dropbox | googledrive
  category        research | personal | backup | uncategorized
  topic           the folder-derived or AI-derived topic label
  filetype        file extension
  year            derived from the file's own mtime, where available
  md5             content hash (dedup key)
  is_duplicate    true if tagged status:duplicate
  duplicate_of    item_key of the canonical copy, if is_duplicate
  mtime           original file modification time (ms since epoch)
  date_added      when this item was created in Zotero

Checkpointed like the other full-library scans in this pipeline - Zotero's
local API is unreliable in one shot at this scale (documented, unfixed
forum issue).
"""
import json
import os
import sys
import time

import duckdb

sys.path.insert(0, "/Users/umashankar/research-toolkit")
from zotero_crud import ZoteroCRUD, _request

DB_PATH = "/Users/umashankar/research-toolkit/document_catalog.duckdb"
CHECKPOINT_FILE = "/Users/umashankar/research-toolkit/zotero_catalog_checkpoint.json"
PAGE_SIZE = 100


def tag_value(tags, prefix):
    for t in tags:
        if t["tag"].startswith(prefix):
            return t["tag"][len(prefix):]
    return None


def has_tag(tags, exact):
    return any(t["tag"] == exact for t in tags)


def source_path(url):
    if not url or "://" not in url:
        return None, None
    source, path = url.split("://", 1)
    return source, path


def fetch_page(zc, start):
    for attempt in range(10):
        status, _, body = _request(
            "GET", f"/users/0/items?limit={PAGE_SIZE}&start={start}",
            headers=zc._headers(), timeout=30,
        )
        if status == 200:
            if not body:
                return []
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                print(f"[RETRY] start={start} attempt={attempt + 1} status=200-but-malformed-body", flush=True)
        else:
            print(f"[RETRY] start={start} attempt={attempt + 1} status={status}", flush=True)
        time.sleep(min(3 * (attempt + 1), 15))
    return None


def main():
    zc = ZoteroCRUD()
    con = duckdb.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            item_key VARCHAR PRIMARY KEY,
            title VARCHAR,
            path VARCHAR,
            source VARCHAR,
            category VARCHAR,
            topic VARCHAR,
            filetype VARCHAR,
            year VARCHAR,
            md5 VARCHAR,
            is_duplicate BOOLEAN,
            duplicate_of VARCHAR,
            mtime BIGINT,
            date_added VARCHAR
        )
    """)

    start = 0
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            start = json.load(f)["start"]
        print(f"Resuming from checkpoint: start={start}", flush=True)

    inserted = 0
    reached_end = False
    while True:
        page = fetch_page(zc, start)
        if page is None:
            print(f"[FAIL] start={start} giving up for now - checkpoint saved, rerun to resume", flush=True)
            break
        if not page:
            reached_end = True
            break

        rows = []
        for it in page:
            d = it["data"]
            source, path = source_path(d.get("url"))
            if source is None:
                continue
            tags = d.get("tags", [])
            duplicate_of_key = tag_value(tags, "duplicate-of:")
            rows.append((
                d["key"], d.get("title"), path, source,
                tag_value(tags, "category:"), tag_value(tags, "topic:"),
                tag_value(tags, "filetype:"), tag_value(tags, "year:"),
                d.get("md5"), has_tag(tags, "status:duplicate"), duplicate_of_key,
                d.get("mtime"), d.get("dateAdded"),
            ))

        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            inserted += len(rows)

        start += PAGE_SIZE
        with open(CHECKPOINT_FILE, "w") as f:
            json.dump({"start": start}, f)
        if start % 1000 == 0:
            print(f"  ...scanned {start}, catalogued {inserted} so far", flush=True)

    if reached_end:
        if os.path.exists(CHECKPOINT_FILE):
            os.remove(CHECKPOINT_FILE)
        print("Building full-text search index over title + path...", flush=True)
        con.execute("INSTALL fts; LOAD fts;")
        con.execute("PRAGMA create_fts_index('documents', 'item_key', 'title', 'path', overwrite=1)")

    total = con.execute("SELECT count(*) FROM documents").fetchone()[0]
    print(f"\n=== CATALOG BUILD DONE: {inserted} rows written this run, {total} total in {DB_PATH} (complete={reached_end}) ===", flush=True)
    con.close()


if __name__ == "__main__":
    main()
