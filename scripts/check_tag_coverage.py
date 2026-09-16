#!/usr/bin/env python3
"""Gate G1: every item with a url (i.e. every item our pipeline created) has
a source: tag. Paginates the whole library and reports the exact gap count -
this is the same cost as running the backfill itself, which is the point:
a real measurement, not a copied number."""
import json
import sys

sys.path.insert(0, "/Users/umashankar/research-toolkit")
from zotero_crud import ZoteroCRUD, _request

PAGE_SIZE = 100


def main():
    zc = ZoteroCRUD()
    start = 0
    total_with_url = 0
    missing = 0
    missing_examples = []

    while True:
        for attempt in range(4):
            status, _, body = _request(
                "GET", f"/users/0/items?limit={PAGE_SIZE}&start={start}",
                headers=zc._headers(), timeout=30,
            )
            if status == 200:
                break
        if status != 200:
            print(f"LIST_FAILED start={start} status={status}", file=sys.stderr)
            sys.exit(2)
        page = json.loads(body) if body else []
        if not page:
            break
        for it in page:
            d = it["data"]
            if not d.get("url"):
                continue
            total_with_url += 1
            tags = {t["tag"] for t in d.get("tags", [])}
            if not any(t.startswith("source:") for t in tags):
                missing += 1
                if len(missing_examples) < 10:
                    missing_examples.append(d.get("url"))
        start += PAGE_SIZE

    print(f"items_with_url={total_with_url} missing_source_tag={missing}")
    for ex in missing_examples:
        print(f"  MISSING: {ex}")

    if missing == 0:
        print("TAG_COVERAGE_COMPLETE")
        sys.exit(0)
    else:
        print(f"TAG_COVERAGE_INCOMPLETE gap={missing}")
        sys.exit(1)


if __name__ == "__main__":
    main()
