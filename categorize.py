"""
Derives Zotero tags from a file's source path for faceted organization:
category (personal/research/backup/uncategorized), topic (clustered from
the source folder), source remote, file type, and year (from mtime).

Used both at item-creation time (zotero_dropbox_import.save_to_zotero) and
by the one-time backfill pass (backfill_tags.py) for items created before
this existed.
"""
import datetime
import os

PERSONAL_KEYWORDS = [
    "personal", "tax", "itr", "pay slip", "payslip", "pay-slip",
    "investment proof", "lic ", "id files", "id proof", "ration card",
    "credential", "resume", " cv", "cv ", "job application", "employment",
    "relieving", "medical", "battery passport", "photo", "camera upload",
    "mobile upload", "screenshot", "coinbase", "wallet",
    "linkedindataexport", "onedrive", "takeout", "classic sites", "vault",
    "aadha", "passport", "indmoney", "hellosign", "meet recording",
    "zoom meeting", "chocolate factory", "potential client",
    "standard-", "standard)", "language proficiency", "forms signed",
    "10th standard", "12th standard",
]

BACKUP_KEYWORDS = [
    "backup", "kopia", "restic", "market-data", "market_data",
    "laptop_backup", "google-cloud-sdk", "coldstore", "ec2",
    "dedup_moved", "repo-branch-archives", "mempalace-backup",
    "avg antivirus", "avg cleaner", "ccleaner", "installer",
    "my mac", "my pc", "mein pc", "other computers", "desktop",
    "my drive",
]

GENERIC_BUCKETS = {
    "pdf", "docx", "pptx", "epub", "doc", "ris", "chm", "mobi", "djvu",
    "documents", "all unorganised files", "zip", "files", "docs",
    "downloads", "saved from web", "saved from chrome", "attachments",
    "",  # root loose files
}


def classify_folder(top_folder):
    name = (top_folder or "").lower()
    if not name:
        return "uncategorized"
    if any(k in name for k in PERSONAL_KEYWORDS):
        return "personal"
    if any(k in name for k in BACKUP_KEYWORDS):
        return "backup"
    if name in {"documents", "all unorganised files", "downloads", "files",
                "docs", "saved from web", "saved from chrome", "attachments",
                "sent files", "new folder", "type", ""}:
        return "uncategorized"
    return "research"


def extract_topic(path):
    """path is a dropbox_path like 'folder/sub/file.ext' or, for
    archive-extracted files, 'folder/sub/archive.zip!internal/file.ext'.
    Uses the top-level folder as the topic, except for generic catch-all
    buckets (pdf/, zip/, Documents/, ...) where the real topical signal is
    one level deeper (e.g. 'zip/Employment(Partial) 3/...'). A path with no
    "/" at all is a loose file sitting at the remote's root - there's no
    folder to cluster by, so it gets its own bucket rather than the
    filename (which would give every root file a unique, useless topic)."""
    segments = path.split("/")
    if len(segments) == 1:
        return "(root)", ""
    top = segments[0]
    if top.lower() in GENERIC_BUCKETS and len(segments) > 2:
        return segments[1], top
    return top, top


def year_from_mtime_ms(mtime_ms):
    if not mtime_ms:
        return None
    try:
        return datetime.datetime.utcfromtimestamp(mtime_ms / 1000).year
    except (ValueError, OSError, OverflowError):
        return None


def build_tags(path, mtime_ms, source):
    topic, top_folder = extract_topic(path)
    category = classify_folder(top_folder)
    ext = os.path.splitext(path.split("!")[-1])[1].lstrip(".").lower() or "unknown"
    tags = [
        {"tag": f"category:{category}"},
        {"tag": f"topic:{topic}"},
        {"tag": f"source:{source}"},
        {"tag": f"filetype:{ext}"},
    ]
    year = year_from_mtime_ms(mtime_ms)
    if year:
        tags.append({"tag": f"year:{year}"})
    return tags
