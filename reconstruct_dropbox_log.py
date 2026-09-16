#!/usr/bin/env python3
"""
The Dropbox import's progress log (zotero_import_log.jsonl) lived in the
session-scoped scratchpad and was lost when the session restarted, along
with the script copy that wrote it. Since every item's url field IS the
record of what got imported (dropbox://<path>), reconstruct a fresh log
from Zotero itself instead of risking 15,000+ duplicate re-imports.

IMPORTANT: scans ALL items and filters by url prefix client-side - do NOT
filter the query by tag=source:dropbox. That tag is only present on items
that have already gone through backfill_tags.py (still incomplete as of
this writing), so tag-filtering silently missed ~95% of real Dropbox items
that predate the tagging fix (a first version of this script did exactly
that and reconstructed only 663 of ~15,912 real entries).

Checkpointed like dedupe_items.py: a full 16,900+ item scan is unreliable
in one shot under Zotero's local API load.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/Users/umashankar/research-toolkit")
from zotero_crud import ZoteroCRUD, _request

OUT_LOG = "/Users/umashankar/research-toolkit/zotero_import_log.jsonl"
CHECKPOINT_FILE = "/Users/umashankar/research-toolkit/zotero_reconstruct_checkpoint.json"
PAGE_SIZE = 100


def main():
    zc = ZoteroCRUD()
    checkpoint = {"start": 0, "reconstructed": 0}
    mode = "w"
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE) as f:
            checkpoint = json.load(f)
        mode = "a"
        print(f"Resuming from checkpoint: start={checkpoint['start']}, {checkpoint['reconstructed']} already reconstructed", flush=True)

    start = checkpoint["start"]
    reconstructed = checkpoint["reconstructed"]
    reached_end = False
    with open(OUT_LOG, mode) as out:
        while True:
            page = None
            for attempt in range(10):
                status, _, body = _request(
                    "GET", f"/users/0/items?limit={PAGE_SIZE}&start={start}",
                    headers=zc._headers(), timeout=30,
                )
                if status == 200:
                    if not body:
                        page = []
                        break
                    try:
                        page = json.loads(body)
                        break
                    except json.JSONDecodeError:
                        print(f"[RETRY] start={start} attempt={attempt + 1} status=200-but-malformed-body", flush=True)
                        page = None
                else:
                    print(f"[RETRY] start={start} attempt={attempt + 1} status={status}", flush=True)
                time.sleep(min(3 * (attempt + 1), 15))
            if page is None:
                print(f"[FAIL] start={start} giving up for now - checkpoint saved, rerun to resume", flush=True)
                break
            if not page:
                reached_end = True
                break
            for it in page:
                d = it["data"]
                url = d.get("url", "")
                if url.startswith("dropbox://"):
                    path = url[len("dropbox://"):]
                    out.write(json.dumps({"path": path, "status": "ok", "size": 0, "time": 0, "reconstructed": True}) + "\n")
                    reconstructed += 1
            start += PAGE_SIZE
            out.flush()
            with open(CHECKPOINT_FILE, "w") as f:
                json.dump({"start": start, "reconstructed": reconstructed}, f)
            if start % 1000 == 0:
                print(f"  ...scanned {start}, reconstructed {reconstructed} so far", flush=True)

    if reached_end and os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)

    print(f"\n=== RECONSTRUCT DONE: {reconstructed} dropbox paths written to {OUT_LOG} (complete={reached_end}) ===", flush=True)


if __name__ == "__main__":
    main()
