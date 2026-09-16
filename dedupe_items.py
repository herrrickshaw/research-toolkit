#!/usr/bin/env python3
"""
Consolidation pass: finds items that are byte-identical duplicates (same
md5, computed at import time from actual file content) and tags the
redundant copies rather than deleting anything - deletion stays a manual,
explicit decision made in Zotero's own UI once duplicates are visible.

For each md5 with more than one item:
  - the earliest-added item (by dateAdded) is treated as canonical
  - every other item in the group gets tagged "status:duplicate" and
    "duplicate-of:<canonical-key>" so they're filterable/reviewable
  - the canonical item gets "has-duplicates:<count>" so it's easy to find
    canonical items worth double-checking

Resumable/idempotent: skips items that already have a "status:duplicate"
or "has-duplicates:" tag matching the current computed state.

Every tag change is logged to DEDUPE_LOG_FILE (JSONL) with the item's
source file location (from its url field), not just its opaque Zotero key -
so "what actually happened to which file" stays auditable without needing
to cross-reference back into Zotero.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/Users/umashankar/research-toolkit")
from zotero_crud import ZoteroCRUD, _request

PAGE_SIZE = 100
DEDUPE_LOG_FILE = os.environ.get("ZOTERO_DEDUPE_LOG", "/Users/umashankar/research-toolkit/zotero_dedupe_log.jsonl")
LISTING_CHECKPOINT_FILE = os.environ.get("ZOTERO_DEDUPE_CHECKPOINT", "/Users/umashankar/research-toolkit/zotero_dedupe_listing_checkpoint.json")


def log_action(rec):
    rec["time"] = time.time()
    with open(DEDUPE_LOG_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


def fetch_all_items(zc):
    """Returns list of {key, version, md5, dateAdded, tags} for every
    attachment item with an md5 (i.e. every item our pipeline created).

    Checkpointed to disk after every page: Zotero's local API has a known,
    unfixed instability under sustained load (confirmed on the Zotero forums)
    that makes a single one-shot pass over 16,000+ items unreliable - a prior
    run gave up partway through more than once, at a different offset each
    time. Restarting from offset 0 every time would just re-cover the same
    partial range forever, so progress is persisted and resumed instead."""
    checkpoint = {"start": 0, "items": []}
    if os.path.exists(LISTING_CHECKPOINT_FILE):
        with open(LISTING_CHECKPOINT_FILE) as f:
            checkpoint = json.load(f)
        print(f"Resuming item listing from checkpoint: start={checkpoint['start']}, {len(checkpoint['items'])} items already collected", flush=True)

    items = checkpoint["items"]
    start = checkpoint["start"]
    reached_end = False
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
                    print(f"[LIST-RETRY] start={start} attempt={attempt + 1} status=200-but-malformed-body", flush=True)
                    page = None
            else:
                print(f"[LIST-RETRY] start={start} attempt={attempt + 1} status={status}", flush=True)
            time.sleep(min(3 * (attempt + 1), 15))
        if page is None:
            print(f"[LIST-FAIL] start={start} giving up for now - checkpoint saved, rerun to resume", flush=True)
            break
        if not page:
            reached_end = True
            break
        for it in page:
            d = it["data"]
            if d.get("md5"):
                items.append({
                    "key": d["key"], "version": d["version"], "md5": d["md5"],
                    "dateAdded": d.get("dateAdded", ""), "tags": d.get("tags", []),
                    "url": d.get("url", ""),
                })
        start += PAGE_SIZE
        with open(LISTING_CHECKPOINT_FILE, "w") as f:
            json.dump({"start": start, "items": items}, f)
        if start % 1000 == 0:
            print(f"  ...listed {start} items so far", flush=True)

    return items, reached_end


def patch_tags(zc, key, version, new_tags):
    status, _, body = _request(
        "PATCH", f"/users/0/items/{key}",
        headers=zc._headers({"Content-Type": "application/json", "If-Unmodified-Since-Version": str(version)}),
        data=json.dumps({"tags": new_tags}).encode(),
        timeout=30,
    )
    return status


def already_marked(tags, prefix):
    return any(t["tag"].startswith(prefix) for t in tags)


def main():
    zc = ZoteroCRUD()
    print("Fetching all items (checkpointed, resumes across restarts if interrupted)...", flush=True)
    items, reached_end = fetch_all_items(zc)
    print(f"Fetched {len(items)} items with md5 (listing {'complete' if reached_end else 'INCOMPLETE - rerun this script to continue'})", flush=True)

    if not reached_end:
        print("Stopping here without tagging: partial item list would miss cross-page duplicate matches and could tag a false canonical. Rerun to resume listing.", flush=True)
        sys.exit(1)

    if os.path.exists(LISTING_CHECKPOINT_FILE):
        os.remove(LISTING_CHECKPOINT_FILE)

    by_md5 = {}
    for it in items:
        by_md5.setdefault(it["md5"], []).append(it)

    dup_groups = {md5: group for md5, group in by_md5.items() if len(group) > 1}
    print(f"Found {len(dup_groups)} groups of duplicate content covering {sum(len(g) for g in dup_groups.values())} items", flush=True)

    updated = skipped = failed = 0
    for md5, group in dup_groups.items():
        group.sort(key=lambda x: x["dateAdded"])
        canonical, dupes = group[0], group[1:]

        if not already_marked(canonical["tags"], "has-duplicates:"):
            new_tags = [t for t in canonical["tags"] if not t["tag"].startswith("has-duplicates:")]
            new_tags.append({"tag": f"has-duplicates:{len(dupes)}"})
            status = patch_tags(zc, canonical["key"], canonical["version"], new_tags)
            if status == 204:
                updated += 1
                log_action({"action": "mark-canonical", "key": canonical["key"], "url": canonical["url"], "duplicate_count": len(dupes)})
            elif status != 412:
                failed += 1
                print(f"[FAIL] canonical {canonical['key']}: {status}", flush=True)
                log_action({"action": "mark-canonical-failed", "key": canonical["key"], "url": canonical["url"], "status": status})
        else:
            skipped += 1

        for dupe in dupes:
            if already_marked(dupe["tags"], "status:duplicate"):
                skipped += 1
                continue
            new_tags = list(dupe["tags"])
            new_tags.append({"tag": "status:duplicate"})
            new_tags.append({"tag": f"duplicate-of:{canonical['key']}"})
            status = patch_tags(zc, dupe["key"], dupe["version"], new_tags)
            if status == 204:
                updated += 1
                log_action({"action": "mark-duplicate", "key": dupe["key"], "url": dupe["url"], "duplicate_of_key": canonical["key"], "duplicate_of_url": canonical["url"]})
                if updated % 100 == 0:
                    print(f"  ... {updated} tagged so far", flush=True)
            elif status == 412:
                skipped += 1
            else:
                failed += 1
                print(f"[FAIL] dupe {dupe['key']}: {status}", flush=True)
                log_action({"action": "mark-duplicate-failed", "key": dupe["key"], "url": dupe["url"], "status": status})
            time.sleep(0.05)

    print(f"\n=== DEDUPE DONE: groups={len(dup_groups)} updated={updated} skipped={skipped} failed={failed} ===", flush=True)


if __name__ == "__main__":
    main()
