#!/usr/bin/env python3
"""Gate G3: a real "Topics" parent collection exists with populated children."""
import json
import sys

sys.path.insert(0, "/Users/umashankar/research-toolkit")
from zotero_crud import ZoteroCRUD, _request


def main():
    zc = ZoteroCRUD()
    status, _, body = _request("GET", "/users/0/collections", headers=zc._headers(), timeout=20)
    if status != 200:
        print(f"LIST_FAILED status={status}", file=sys.stderr)
        sys.exit(2)
    collections = json.loads(body) if body else []

    topics_parent = next((c for c in collections if c["data"]["name"] == "Topics" and not c["data"].get("parentCollection")), None)
    if not topics_parent:
        print("TOPICS_COLLECTION_MISSING")
        sys.exit(1)

    parent_key = topics_parent["data"]["key"]
    children = [c for c in collections if c["data"].get("parentCollection") == parent_key]
    print(f"Topics parent key={parent_key}, children={len(children)}")

    if len(children) == 0:
        print("TOPICS_COLLECTION_EMPTY")
        sys.exit(1)

    populated = 0
    for child in children[:5]:
        status, _, body = _request(
            "GET", f"/users/0/collections/{child['data']['key']}/items?limit=1",
            headers=zc._headers(), timeout=20,
        )
        if status == 200:
            items = json.loads(body) if body else []
            if items:
                populated += 1

    print(f"sampled {min(5, len(children))} children, {populated} populated")
    if populated == 0:
        print("TOPICS_COLLECTION_UNPOPULATED")
        sys.exit(1)

    print("TOPICS_COLLECTION_BUILT")
    sys.exit(0)


if __name__ == "__main__":
    main()
