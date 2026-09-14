#!/usr/bin/env python3
"""
Remediates files that failed in zotero_dropbox_import.py (mostly HTTP timeouts
talking to Zotero on large files - not real Dropbox errors).

Uses Zotero's real local CRUD API (zotero_crud.py) so we can:
  - file items directly into the configured collection explicitly
  - retry just the upload step independently of item creation

For each currently-failed path:
  1. Re-download the file via rclone (most source folders won't be in a
     locally-synced copy, so we can't just link a local path).
  2. Parse the filename for title/author(s)/year (many are ebook-style names:
     "(Series) Author - Title (Year)-Publisher.pdf").
  3. Look up the real bibliographic record from Open Library (primary) or
     Google Books (fallback) - both free, no API key - to confirm/enrich
     title, authors, publisher, date, ISBN.
  4. Create a "book" item via CRUD, then upload the file as its attachment.
  5. Append a "status": "ok" record to the shared log so the bulk script's
     resume logic won't reprocess (and duplicate) these paths.
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zotero_dropbox_import import download, STAGING_DIR, LOG_FILE, DROPBOX_IMPORT_COLLECTION_KEY
from zotero_crud import ZoteroCRUD

GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"


def load_log():
    ok, fail = set(), {}
    if not os.path.exists(LOG_FILE):
        return ok, fail
    with open(LOG_FILE) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "ok":
                ok.add(rec["path"])
                fail.pop(rec["path"], None)
            elif rec.get("status") == "fail" and rec["path"] not in ok:
                fail[rec["path"]] = rec
    return ok, fail


def log(rec):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")


NAME_RE = re.compile(
    r"^(?:\([^)]*\)\s*)?"
    r"(?P<authors>[^-]+?)\s*-\s*"
    r"(?P<title>.+?)"
    r"(?:\s*\((?P<year>(19|20)\d{2})\))?"
    r"(?:\s*-\s*[^-]+)?$"
)


def parse_filename(basename):
    stem = os.path.splitext(basename)[0]
    m = NAME_RE.match(stem)
    if not m:
        return {"title": stem, "authors": [], "year": None}
    authors_raw = m.group("authors").strip(" ,")
    authors = []
    for part in re.split(r",| and |;", authors_raw):
        part = part.strip()
        if not part or part.lower() in ("et al", "et al."):
            continue
        bits = part.split()
        if len(bits) >= 2:
            authors.append({"lastName": bits[-1], "firstName": " ".join(bits[:-1]), "creatorType": "author"})
        elif bits:
            authors.append({"lastName": bits[0], "firstName": "", "creatorType": "author"})
    title = re.sub(r"[_]+", " ", m.group("title")).strip(" -_")
    return {"title": title or stem, "authors": authors, "year": m.group("year")}


def openlibrary_lookup(title, authors):
    params = {"title": title, "limit": 1, "fields": "title,author_name,first_publish_year,publisher,isbn"}
    if authors:
        params["author"] = authors[0]["lastName"]
    url = f"https://openlibrary.org/search.json?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"    [open library lookup failed: {e}]")
        return None
    docs = data.get("docs") or []
    if not docs:
        return None
    doc = docs[0]
    return {
        "title": doc.get("title"),
        "authors": doc.get("author_name", []),
        "publisher": (doc.get("publisher") or [None])[0],
        "date": str(doc["first_publish_year"]) if doc.get("first_publish_year") else None,
        "isbn": (doc.get("isbn") or [None])[0],
        "abstract": None,
    }


def google_books_lookup(title, authors):
    query = f"intitle:{title}"
    if authors:
        query += f"+inauthor:{authors[0]['lastName']}"
    url = f"{GOOGLE_BOOKS_URL}?{urllib.parse.urlencode({'q': query, 'maxResults': 1})}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"    [google books lookup failed: {e}]")
        return None
    items = data.get("items") or []
    if not items:
        return None
    info = items[0].get("volumeInfo", {})
    return {
        "title": info.get("title"),
        "authors": info.get("authors", []),
        "publisher": info.get("publisher"),
        "date": info.get("publishedDate"),
        "isbn": next(
            (i["identifier"] for i in info.get("industryIdentifiers", []) if i["type"] in ("ISBN_13", "ISBN_10")),
            None,
        ),
        "abstract": info.get("description"),
    }


def build_creators(google_authors, fallback_authors):
    if google_authors:
        creators = []
        for name in google_authors:
            bits = name.split()
            if len(bits) >= 2:
                creators.append({"creatorType": "author", "firstName": " ".join(bits[:-1]), "lastName": bits[-1]})
            else:
                creators.append({"creatorType": "author", "lastName": name, "firstName": ""})
        return creators
    return fallback_authors


def main():
    ok, fail = load_log()
    print(f"Already ok: {len(ok)}  Currently failing: {len(fail)}", flush=True)
    os.makedirs(STAGING_DIR, exist_ok=True)
    zc = ZoteroCRUD()

    for path in list(fail.keys()):
        basename = os.path.basename(path)
        local_path = os.path.join(STAGING_DIR, f"remediate_{uuid.uuid4().hex}_{basename}")

        dl_ok, dl_err = download(path, local_path)
        if not dl_ok:
            print(f"[DL-FAIL] {basename}: {dl_err}", flush=True)
            log({"path": path, "status": "fail", "stage": "remediate-download", "error": dl_err, "time": time.time()})
            continue

        parsed = parse_filename(basename)
        enriched = openlibrary_lookup(parsed["title"], parsed["authors"]) or google_books_lookup(parsed["title"], parsed["authors"])
        if enriched:
            title = enriched["title"] or parsed["title"]
            creators = build_creators(enriched["authors"], parsed["authors"])
            date = enriched["date"] or parsed["year"] or ""
            publisher, isbn, abstract = enriched["publisher"], enriched["isbn"], enriched["abstract"]
            source = "google-books"
        else:
            title, creators = parsed["title"], parsed["authors"]
            date = parsed["year"] or ""
            publisher = isbn = abstract = None
            source = "filename-only"

        item = {
            "itemType": "book",
            "title": title,
            "creators": creators,
            "date": date,
            "publisher": publisher or "",
            "ISBN": isbn or "",
            "abstractNote": abstract or "",
            "url": f"dropbox://{path}",
            "tags": [{"tag": "dropbox-import-remediated"}],
        }
        if DROPBOX_IMPORT_COLLECTION_KEY:
            item["collections"] = [DROPBOX_IMPORT_COLLECTION_KEY]

        status, resp = zc.create_items([item])
        item_key = None
        if status == 200:
            item_key = resp.get("success", {}).get("0")
        if not item_key:
            print(f"[ITEM-FAIL] {basename}: {status} {json.dumps(resp)[:200]}", flush=True)
            log({"path": path, "status": "fail", "stage": "remediate-item", "error": json.dumps(resp)[:300], "time": time.time()})
            os.remove(local_path)
            continue

        with open(local_path, "rb") as f:
            content = f.read()
        md5 = hashlib.md5(content).hexdigest()
        filesize = len(content)
        mtime_ms = int(os.path.getmtime(local_path) * 1000)

        att_status, resp2 = zc.create_items([{
            "itemType": "attachment",
            "parentItem": item_key,
            "linkMode": "imported_file",
            "title": basename,
            "filename": basename,
            "contentType": "application/pdf",
            "md5": md5,
            "mtime": mtime_ms,
            "tags": [],
        }])
        att_key = resp2.get("success", {}).get("0") if att_status == 200 else None
        if not att_key:
            print(f"[ATTACH-ITEM-FAIL] {basename}: {att_status} {json.dumps(resp2)[:200]}", flush=True)
            log({"path": path, "status": "fail", "stage": "remediate-attach-item", "error": json.dumps(resp2)[:300], "time": time.time()})
            os.remove(local_path)
            continue

        up_ok, up_status, up_body = zc.upload_attachment_file(att_key, local_path, basename, "application/pdf", md5, mtime_ms, filesize)
        os.remove(local_path)

        if up_ok:
            print(f"[OK:{source}] {title} ({date}) <- {basename}", flush=True)
            log({"path": path, "status": "ok", "remediated": True, "metadata_source": source, "time": time.time()})
        else:
            print(f"[UPLOAD-FAIL] {basename}: {up_status} {up_body}", flush=True)
            log({"path": path, "status": "fail", "stage": "remediate-upload", "error": str(up_body)[:300], "time": time.time()})

        time.sleep(0.5)


if __name__ == "__main__":
    main()
