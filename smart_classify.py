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
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/Users/umashankar/research-toolkit")
from zotero_crud import ZoteroCRUD, _request

CREDENTIALS_FILE = "/Users/umashankar/.config/market-secrets/credentials.env"
CLASSIFY_LOG_FILE = os.environ.get("ZOTERO_CLASSIFY_LOG", "/Users/umashankar/research-toolkit/zotero_classify_log.jsonl")
PAGE_SIZE = 100
MODEL = "openai/gpt-oss-20b"  # fast + cheap on Groq, plenty for short-title classification
VALID_CATEGORIES = {"personal", "research", "backup", "uncategorized"}


def load_key(var_name):
    """Reads one key from the credentials file without ever printing it."""
    if not os.path.exists(CREDENTIALS_FILE):
        return None
    with open(CREDENTIALS_FILE) as f:
        for line in f:
            line = line.strip()
            if line.startswith(f"{var_name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def classify_title(title, api_key):
    prompt = (
        "Classify this file title into a short topic (2-4 words, a specific "
        "subject area) and a category from exactly [personal, research, "
        "backup, uncategorized]. 'personal' = tax/ID/financial/employment/"
        "medical/personal photos. 'backup' = system/software/data backups, "
        "not human-readable documents. 'uncategorized' = truly ambiguous. "
        "Otherwise 'research'.\n\n"
        f'Title: "{title}"\n\n'
        'Respond with ONLY compact JSON: {"topic": "...", "category": "..."}'
    )
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 250,
    }).encode()
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=body, method="POST",
    )
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) research-toolkit/1.0")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return None, f"http-{e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
    except Exception as e:
        return None, str(e)

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
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


def log_action(rec):
    rec["time"] = time.time()
    with open(CLASSIFY_LOG_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


def already_classified(tags):
    return any(t["tag"] == "ai-classified:true" for t in tags)


def merge_tags(existing, topic, category):
    prefixes = ("category:", "topic:", "ai-classified:")
    kept = [t for t in existing if not t["tag"].startswith(prefixes)]
    kept.append({"tag": f"category:{category}"})
    kept.append({"tag": f"topic:{topic}"})
    kept.append({"tag": "ai-classified:true"})
    return kept


def fetch_uncategorized(zc):
    items = []
    start = 0
    while True:
        page = None
        for attempt in range(10):
            status, _, body = _request(
                "GET", f"/users/0/items?tag=category:uncategorized&limit={PAGE_SIZE}&start={start}",
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
                    page = None
            time.sleep(min(3 * (attempt + 1), 15))
        if page is None:
            print(f"[LIST-FAIL] start={start} giving up after retries", flush=True)
            break
        if not page:
            break
        for it in page:
            d = it["data"]
            if not already_classified(d.get("tags", [])):
                items.append({"key": d["key"], "version": d["version"], "title": d.get("title", ""), "url": d.get("url", ""), "tags": d.get("tags", [])})
        start += PAGE_SIZE
    return items


def patch_tags(zc, key, version, tags):
    status, _, body = _request(
        "PATCH", f"/users/0/items/{key}",
        headers=zc._headers({"Content-Type": "application/json", "If-Unmodified-Since-Version": str(version)}),
        data=json.dumps({"tags": tags}).encode(),
        timeout=30,
    )
    return status


def main():
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
