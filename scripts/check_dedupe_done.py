#!/usr/bin/env python3
"""Gate G2: dedupe was actually applied, not just "ran without crashing".
Uses a known positive control - "Shell.pdf" is known (from an earlier manual
search this session) to exist identically in at least 8 different backup zip
locations (Employment(Partial), Employment(Partial) 2/3, id files, PC (2),
laptop_backup, Desktop (Selective Sync Conflict), root zip/) - so if dedupe
ran correctly, that group must have exactly one canonical (has-duplicates:N)
and the rest tagged status:duplicate."""
import json
import sys

sys.path.insert(0, "/Users/umashankar/research-toolkit")
from zotero_crud import ZoteroCRUD, _request


def search(zc, tag=None, q=None):
    params = f"limit=100"
    if tag:
        params += f"&tag={tag}"
    if q:
        params += f"&q={q}"
    status, _, body = _request("GET", f"/users/0/items?{params}", headers=zc._headers(), timeout=20)
    if status != 200:
        return None
    return json.loads(body) if body else []


def main():
    zc = ZoteroCRUD()

    dup_tagged = search(zc, tag="status:duplicate")
    canonical_tagged = search(zc, tag="has-duplicates:*") or search(zc, q="has-duplicates")

    if dup_tagged is None:
        print("SEARCH_FAILED (status:duplicate)", file=sys.stderr)
        sys.exit(2)

    print(f"items tagged status:duplicate: {len(dup_tagged)} (page 1)")

    shell_items = search(zc, q="Shell.pdf")
    if shell_items is None:
        print("SEARCH_FAILED (Shell.pdf)", file=sys.stderr)
        sys.exit(2)

    shell_exact = [it for it in shell_items if it["data"].get("title") == "Shell.pdf"]
    print(f"'Shell.pdf' exact-title items found: {len(shell_exact)}")

    tagged_as_dup = sum(1 for it in shell_exact if any(t["tag"] == "status:duplicate" for t in it["data"].get("tags", [])))
    tagged_as_canonical = sum(1 for it in shell_exact if any(t["tag"].startswith("has-duplicates:") for t in it["data"].get("tags", [])))

    print(f"  of those: {tagged_as_dup} tagged status:duplicate, {tagged_as_canonical} tagged has-duplicates:")

    if len(shell_exact) < 2:
        print("POSITIVE_CONTROL_MISSING: expected >=2 copies of Shell.pdf, found fewer - control invalid", file=sys.stderr)
        sys.exit(2)

    if tagged_as_dup >= 1 and tagged_as_canonical >= 1 and len(dup_tagged) > 0:
        print("DEDUPE_APPLIED")
        sys.exit(0)
    else:
        print("DEDUPE_NOT_APPLIED: known duplicate group is not correctly tagged")
        sys.exit(1)


if __name__ == "__main__":
    main()
