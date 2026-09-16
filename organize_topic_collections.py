#!/usr/bin/env python3
"""
Complements the topic: tags (searchable/filterable) with an actual browsable
collection hierarchy: a parent "Topics" collection with one child collection
per distinct topic: tag value, each populated with the items that carry
that tag. Tags stay the primary organization; this is just a folder-view
convenience on top of the same data.

Resumable/idempotent: re-running only adds items to collections they're not
already in, and reuses existing per-topic collections rather than
duplicating them.

Every collection assignment is logged to ORGANIZE_LOG_FILE (JSONL) with the
item's source file location (from its url field), so "which file went into
which topic collection" stays auditable.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/Users/umashankar/research-toolkit")
from zotero_crud import ZoteroCRUD, _request

PAGE_SIZE = 100
TOPICS_PARENT_NAME = "Topics"
ORGANIZE_LOG_FILE = os.environ.get("ZOTERO_ORGANIZE_LOG", "/Users/umashankar/research-toolkit/zotero_organize_log.jsonl")


def log_action(rec):
    rec["time"] = time.time()
    with open(ORGANIZE_LOG_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


def fetch_all_collections(zc):
    """One retried fetch, cached and reused across every get_or_create_collection
    call - the original re-fetched ALL collections from scratch on every one
    of up to 300 calls, which was both needlessly expensive and had no retry
    (a bare timeout there crashed the whole run with no progress saved)."""
    for attempt in range(10):
        status, _, body = _request("GET", "/users/0/collections", headers=zc._headers(), timeout=30)
        if status == 200:
            if not body:
                return []
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                print(f"[COLLECTIONS-RETRY] attempt={attempt + 1} status=200-but-malformed-body", flush=True)
        else:
            print(f"[COLLECTIONS-RETRY] attempt={attempt + 1} status={status}", flush=True)
        time.sleep(min(3 * (attempt + 1), 15))
    print("[COLLECTIONS-FAIL] giving up, treating as empty", flush=True)
    return []


def get_or_create_collection(zc, name, parent_key, cache):
    """cache: dict of (name, parent_key) -> collection key, mutated in place
    as new collections are created, so repeated calls for the same topic
    (or a re-run after a partial failure) don't recreate it."""
    cache_key = (name, parent_key)
    if cache_key in cache:
        return cache[cache_key]

    payload = [{"name": name, **({"parentCollection": parent_key} if parent_key else {})}]
    for attempt in range(10):
        status, _, body = _request(
            "POST", "/users/0/collections",
            headers=zc._headers({"Content-Type": "application/json"}),
            data=json.dumps(payload).encode(), timeout=30,
        )
        if status == 200 and body:
            try:
                key = json.loads(body)["success"]["0"]
                cache[cache_key] = key
                return key
            except (json.JSONDecodeError, KeyError):
                print(f"[CREATE-RETRY] {name!r} attempt={attempt + 1} status=200-but-malformed-body", flush=True)
        else:
            print(f"[CREATE-RETRY] {name!r} attempt={attempt + 1} status={status}", flush=True)
        time.sleep(min(3 * (attempt + 1), 15))
    raise RuntimeError(f"failed to create collection {name!r} after retries: {status} {body[:200]}")


ITEMS_CHECKPOINT_FILE = "/Users/umashankar/research-toolkit/zotero_organize_listing_checkpoint.json"


def fetch_all_items(zc):
    """Checkpointed like dedupe_items.py's listing pass - a full library scan
    is unreliable in one shot under Zotero's local API load, so progress is
    persisted and resumed rather than silently returning a partial list."""
    checkpoint = {"start": 0, "items": []}
    if os.path.exists(ITEMS_CHECKPOINT_FILE):
        with open(ITEMS_CHECKPOINT_FILE) as f:
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
            topics = [t["tag"][len("topic:"):] for t in d.get("tags", []) if t["tag"].startswith("topic:")]
            if topics:
                items.append({
                    "key": d["key"], "version": d["version"],
                    "collections": d.get("collections", []), "topic": topics[0],
                    "url": d.get("url", ""),
                })
        start += PAGE_SIZE
        with open(ITEMS_CHECKPOINT_FILE, "w") as f:
            json.dump({"start": start, "items": items}, f)
        if start % 1000 == 0:
            print(f"  ...listed {start} items so far", flush=True)

    if reached_end and os.path.exists(ITEMS_CHECKPOINT_FILE):
        os.remove(ITEMS_CHECKPOINT_FILE)
    return items, reached_end


def add_to_collection(zc, item_key, version, current_collections, collection_key):
    if collection_key in current_collections:
        return "already-in"
    new_collections = current_collections + [collection_key]
    status, _, body = _request(
        "PATCH", f"/users/0/items/{item_key}",
        headers=zc._headers({"Content-Type": "application/json", "If-Unmodified-Since-Version": str(version)}),
        data=json.dumps({"collections": new_collections}).encode(), timeout=30,
    )
    if status == 204:
        return "ok"
    if status == 412:
        return "conflict"
    return f"fail:{status}"


def main():
    zc = ZoteroCRUD()

    print("Fetching existing collections (once, cached)...", flush=True)
    existing = fetch_all_collections(zc)
    cache = {(c["data"]["name"], c["data"].get("parentCollection") or None): c["data"]["key"] for c in existing}
    print(f"  {len(cache)} existing collections loaded into cache", flush=True)

    print(f"Ensuring parent collection {TOPICS_PARENT_NAME!r} exists...", flush=True)
    parent_key = get_or_create_collection(zc, TOPICS_PARENT_NAME, None, cache)

    print("Fetching all items with a topic: tag (checkpointed, resumes across restarts)...", flush=True)
    items, reached_end = fetch_all_items(zc)
    print(f"Fetched {len(items)} items across {len(set(i['topic'] for i in items))} distinct topics (listing {'complete' if reached_end else 'INCOMPLETE - rerun this script to continue'})", flush=True)

    if not reached_end:
        print("Stopping here without filing: rerun to resume listing and get the complete item set before filing into collections.", flush=True)
        sys.exit(1)

    ok = already = conflict = failed = 0

    for it in items:
        topic = it["topic"]
        try:
            collection_key = get_or_create_collection(zc, topic, parent_key, cache)
        except RuntimeError as e:
            failed += 1
            print(f"[FAIL] collection for topic {topic!r}: {e}", flush=True)
            log_action({"action": "filed-failed", "key": it["key"], "url": it["url"], "topic": topic, "result": str(e)})
            continue

        result = add_to_collection(zc, it["key"], it["version"], it["collections"], collection_key)
        if result == "ok":
            ok += 1
            log_action({"action": "filed", "key": it["key"], "url": it["url"], "topic": topic, "collection_key": collection_key})
            if ok % 100 == 0:
                print(f"  ... {ok} items filed so far", flush=True)
        elif result == "already-in":
            already += 1
        elif result == "conflict":
            conflict += 1
        else:
            failed += 1
            print(f"[FAIL] {it['key']}: {result}", flush=True)
            log_action({"action": "filed-failed", "key": it["key"], "url": it["url"], "topic": topic, "result": result})
        time.sleep(0.05)

    print(f"\n=== TOPIC ORGANIZE DONE: filed={ok} already-in={already} conflict={conflict} failed={failed} topics={len(cache) - 1} ===", flush=True)


if __name__ == "__main__":
    main()
