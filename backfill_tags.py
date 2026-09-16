#!/usr/bin/env python3
"""
One-time (but safely re-runnable) pass that adds category/topic/source/
filetype/year tags to every attachment item already created by
zotero_dropbox_import.py / process_archives.py before build_tags() existed
(or that got a buggy topic tag from the root-loose-file bug fixed in
categorize.py).

Idempotent: re-derives the correct tag set for every item and only PATCHes
items whose tags differ from what they should be - safe to interrupt and
rerun, and self-heals items that got fixed-forward logic changes since they
were first tagged (like the root-file topic bug).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, "/Users/umashankar/research-toolkit")
from categorize import build_tags
from zotero_crud import ZoteroCRUD, _request

PAGE_SIZE = 100
LISTING_CHECKPOINT_FILE = "/Users/umashankar/research-toolkit/zotero_backfill_listing_checkpoint.json"


def source_from_url(url):
    if not url:
        return None, None
    if "://" not in url:
        return None, None
    source, path = url.split("://", 1)
    return source, path


def derived_category_tags(url, mtime_ms):
    source, path = source_from_url(url)
    if source is None:
        return None
    return build_tags(path, mtime_ms, source)


def tags_need_update(existing_tags, derived):
    existing_set = {t["tag"] for t in existing_tags}
    derived_set = {t["tag"] for t in derived}
    our_prefixes = ("category:", "topic:", "source:", "filetype:", "year:")
    existing_ours = {t for t in existing_set if t.startswith(our_prefixes)}
    return existing_ours != derived_set


def merge_tags(existing_tags, derived):
    our_prefixes = ("category:", "topic:", "source:", "filetype:", "year:")
    kept = [t for t in existing_tags if not t["tag"].startswith(our_prefixes)]
    return kept + derived


def patch_item_tags(key, version, new_tags, server_id, api_key):
    body = json.dumps({"tags": new_tags}).encode()
    status, headers, resp_body = _request(
        "PATCH", f"/users/0/items/{key}",
        headers={
            "Content-Type": "application/json",
            "Zotero-API-Key": api_key,
            "Zotero-Server-ID": server_id,
            "If-Unmodified-Since-Version": str(version),
        },
        data=body,
        timeout=30,
    )
    return status


def main():
    zc = ZoteroCRUD()
    start = 0
    if os.path.exists(LISTING_CHECKPOINT_FILE):
        with open(LISTING_CHECKPOINT_FILE) as f:
            start = json.load(f)["start"]
        print(f"Resuming from checkpoint: start={start} (each item is tagged immediately as it's scanned, so this just skips the already-covered range rather than re-scanning it)", flush=True)
    total_seen = total_updated = total_skipped = total_failed = 0

    reached_end = False
    while True:
        items = None
        for list_attempt in range(10):
            status, headers, body = _request(
                "GET", f"/users/0/items?limit={PAGE_SIZE}&start={start}",
                headers=zc._headers(),
                timeout=30,
            )
            if status == 200:
                if not body:
                    items = []
                    break
                try:
                    items = json.loads(body)
                    break
                except json.JSONDecodeError:
                    print(f"[LIST-RETRY] start={start} attempt={list_attempt + 1} status=200-but-malformed-body", flush=True)
                    items = None
            else:
                print(f"[LIST-RETRY] start={start} attempt={list_attempt + 1} status={status} body={body[:150]}", flush=True)
            time.sleep(min(3 * (list_attempt + 1), 15))
        if items is None:
            print(f"[LIST-FAIL] start={start} giving up after retries - checkpoint saved, rerun to resume", flush=True)
            break
        if not items:
            reached_end = True
            break

        for it in items:
            d = it["data"]
            total_seen += 1
            derived = derived_category_tags(d.get("url"), d.get("mtime"))
            if derived is None:
                total_skipped += 1
                continue
            existing = d.get("tags", [])
            if not tags_need_update(existing, derived):
                total_skipped += 1
                continue

            new_tags = merge_tags(existing, derived)
            patch_status = patch_item_tags(d["key"], d["version"], new_tags, zc.server_id, zc.key)
            if patch_status == 204:
                total_updated += 1
                if total_updated % 100 == 0:
                    print(f"... {total_updated} updated so far (seen {total_seen})", flush=True)
            elif patch_status == 412:
                # Version changed under us (another job touched it since listing) - safe to skip, will be picked up next pass
                total_skipped += 1
            else:
                total_failed += 1
                print(f"[PATCH-FAIL] {d['key']}: {patch_status}", flush=True)
            time.sleep(0.05)

        start += PAGE_SIZE
        with open(LISTING_CHECKPOINT_FILE, "w") as f:
            json.dump({"start": start}, f)

    if reached_end and os.path.exists(LISTING_CHECKPOINT_FILE):
        os.remove(LISTING_CHECKPOINT_FILE)

    print(f"\n=== BACKFILL DONE: seen={total_seen} updated={total_updated} skipped={total_skipped} failed={total_failed} complete={reached_end} ===", flush=True)


if __name__ == "__main__":
    main()
