#!/usr/bin/env python3
"""
Content-based classification for items our folder-name heuristic couldn't
place meaningfully (category:uncategorized - things like Documents/, pdf/,
(root), all unorganised files/). Unlike the heuristic, this actually reads
the item's title and asks an LLM for a real topic + category, the same
metadata-only approach the Autotag Zotero plugin uses (no PDF/full-text
sent anywhere).

Uses Groq (fast, cheap, OpenAI-compatible chat API) by default. The key is
read directly from the user's existing credentials file and never printed
or logged - only used in an Authorization header.

Every classification is logged to CLASSIFY_LOG_FILE (JSONL) with the file's
source location, model used, and the raw model response, for audit and so
a bad classification can be traced back and corrected.

Resumable/idempotent: skips items already tagged "ai-classified:true".
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

# Run as a script from anywhere: make the sibling zotero_crud module importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zotero_crud import ZoteroCRUD, _request  # noqa: E402

CREDENTIALS_FILE = os.environ.get("CLASSIFY_CREDENTIALS_FILE", "/Users/umashankar/.config/market-secrets/credentials.env")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
LIST_RETRIES = 10
CLASSIFY_LOG_FILE = os.environ.get("ZOTERO_CLASSIFY_LOG", "/Users/umashankar/research-toolkit/zotero_classify_log.jsonl")
PAGE_SIZE = 100
MODEL = "openai/gpt-oss-20b"  # fast + cheap on Groq, plenty for short-title classification
VALID_CATEGORIES = {"personal", "research", "backup", "uncategorized"}


def load_key(var_name: str, credentials_file: str | None = None) -> Optional[str]:
    """Reads one key from the credentials file without ever printing it."""
    credentials_file = credentials_file or CREDENTIALS_FILE
    if not os.path.exists(credentials_file):
        return None
    with open(credentials_file) as f:
        for line in f:
            line = line.strip()
            if line.startswith(f"{var_name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def build_prompt(title: str) -> str:
    """The single-turn classification prompt for one file title."""
    return (
        "Classify this file title into a short topic (2-4 words, a specific "
        "subject area) and a category from exactly [personal, research, "
        "backup, uncategorized]. 'personal' = tax/ID/financial/employment/"
        "medical/personal photos. 'backup' = system/software/data backups, "
        "not human-readable documents. 'uncategorized' = truly ambiguous. "
        "Otherwise 'research'.\n\n"
        f'Title: "{title}"\n\n'
        'Respond with ONLY compact JSON: {"topic": "...", "category": "..."}'
    )


def parse_classification(content: str) -> tuple[Optional[dict], Optional[str]]:
    """Model reply text -> ({"topic","category"}, None) or (None, error string)."""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return None, f"no-json-in-response: {content[:200]}"
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None, f"bad-json: {content[:200]}"

    category = parsed.get("category", "").strip().lower()
    topic = parsed.get("topic", "").strip()
    if category not in VALID_CATEGORIES or not topic:
        return None, f"invalid-fields: {parsed}"
    return {"topic": topic, "category": category}, None


def groq_chat(body: bytes, api_key: str) -> dict:
    """POST one chat-completion request to Groq; raises on HTTP/network errors."""
    req = urllib.request.Request(GROQ_URL, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) research-toolkit/1.0")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def classify_title(title: str, api_key: str,
                   chat: Callable[[bytes, str], dict] = groq_chat) -> tuple[Optional[dict], Optional[str]]:
    """One title -> ({"topic","category"}, None) or (None, error). `chat` is injectable for tests."""
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": build_prompt(title)}],
        "temperature": 0,
        "max_tokens": 250,
    }).encode()
    try:
        data = chat(body, api_key)
    except urllib.error.HTTPError as e:
        return None, f"http-{e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:  # noqa: BLE001 - any transport failure becomes a logged per-item fallback
        return None, str(e)

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return parse_classification(content)


def log_action(rec: dict) -> None:
    """Append one audit record (adds a timestamp) to CLASSIFY_LOG_FILE."""
    rec["time"] = time.time()
    with open(CLASSIFY_LOG_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


def already_classified(tags: list) -> bool:
    return any(t["tag"] == "ai-classified:true" for t in tags)


def merge_tags(existing: list, topic: str, category: str) -> list:
    """Replace category:/topic:/ai-classified: tags, keep all others."""
    prefixes = ("category:", "topic:", "ai-classified:")
    kept = [t for t in existing if not t["tag"].startswith(prefixes)]
    kept.append({"tag": f"category:{category}"})
    kept.append({"tag": f"topic:{topic}"})
    kept.append({"tag": "ai-classified:true"})
    return kept


def fetch_page(zc, start: int, sleep: Callable[[float], None] = time.sleep) -> Optional[list]:
    """One page of category:uncategorized items; None after LIST_RETRIES failed attempts."""
    for attempt in range(LIST_RETRIES):
        status, _, body = _request(
            "GET", f"/users/0/items?tag=category:uncategorized&limit={PAGE_SIZE}&start={start}",
            headers=zc._headers(), timeout=30,
        )
        if status == 200:
            if not body:
                return []
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                pass
        sleep(min(3 * (attempt + 1), 15))
    return None


def fetch_uncategorized(zc, sleep: Callable[[float], None] = time.sleep) -> list:
    """All not-yet-AI-classified items tagged category:uncategorized, as flat dicts."""
    items = []
    start = 0
    while True:
        page = fetch_page(zc, start, sleep)
        if page is None:
            print(f"[LIST-FAIL] start={start} giving up after retries", flush=True)
            break
        if not page:
            break
        for it in page:
            d = it["data"]
            if not already_classified(d.get("tags", [])):
                items.append({
                    "key": d["key"], "version": d["version"], "title": d.get("title", ""),
                    "url": d.get("url", ""), "tags": d.get("tags", []),
                })
        start += PAGE_SIZE
    return items


def patch_tags(zc, key: str, version: int, tags: list) -> Optional[int]:
    """PATCH an item's tags; returns the HTTP status (204 on success)."""
    status, _, body = _request(
        "PATCH", f"/users/0/items/{key}",
        headers=zc._headers({"Content-Type": "application/json", "If-Unmodified-Since-Version": str(version)}),
        data=json.dumps({"tags": tags}).encode(),
        timeout=30,
    )
    return status


def main() -> None:
    """CLI: classify every uncategorized Zotero item by title and patch its tags."""
    api_key = load_key("GROQ_API_KEY")
    if not api_key:
        print("No GROQ_API_KEY found in credentials file.", file=sys.stderr)
        sys.exit(1)

    zc = ZoteroCRUD()
    print("Fetching items tagged category:uncategorized...", flush=True)
    items = fetch_uncategorized(zc)
    print(f"Found {len(items)} items to classify", flush=True)

    classified = failed = 0
    for it in items:
        result, err = classify_title(it["title"] or os.path.basename(it["url"]), api_key)
        if result is None:
            # Genuinely unclassifiable from the title alone (e.g. a bare scan
            # filename with no semantic content) - fall back rather than
            # retry forever on a title that will fail the same way every time.
            result = {"topic": "unclear", "category": "uncategorized"}
            log_action({"key": it["key"], "url": it["url"], "title": it["title"], "status": "fallback", "error": err})

        new_tags = merge_tags(it["tags"], result["topic"], result["category"])
        status = patch_tags(zc, it["key"], it["version"], new_tags)
        if status == 204:
            classified += 1
            log_action({"key": it["key"], "url": it["url"], "title": it["title"], "status": "ok", "result": result, "model": MODEL})
            if classified % 25 == 0:
                print(f"  ... {classified} classified so far", flush=True)
        else:
            failed += 1
            log_action({"key": it["key"], "url": it["url"], "title": it["title"], "status": "fail", "error": f"patch-{status}"})
        time.sleep(0.6)  # conservative rate limit

    print(f"\n=== CLASSIFY DONE: classified={classified} failed={failed} ===", flush=True)


if __name__ == "__main__":
    main()
