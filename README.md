# research-toolkit

Bulk-import PDFs, ebooks, and other documents from a cloud storage remote (Dropbox, Google Drive, etc. via [rclone](https://rclone.org)) into a locally running [Zotero](https://www.zotero.org) library — including documents buried inside `.zip`/`.tar` archives — using Zotero 10's local CRUD API.

## Why not Zotero's browser-connector API?

Zotero's connector endpoint (`/connector/saveStandaloneAttachment`) is designed for one-off "save this page" actions from the browser extension. Under sustained bulk load (hundreds of sequential saves) it degrades — first as slow timeouts on large files, eventually as outright `500` errors from accumulated session state. Zotero 10 also ships a proper CRUD-capable REST API mirroring the real [Zotero Web API](https://www.zotero.org/support/dev/web_api/v3/basics) at `http://127.0.0.1:23119/api/`, which is what this toolkit uses instead — stable under load, and it lets you target a specific collection explicitly rather than "whatever's selected in the Zotero window right now."

## Setup

1. **rclone remote** for your cloud storage: `rclone config` (see [rclone docs](https://rclone.org/docs/)).
2. **Enable Zotero's local API** — it's off by default:
   Zotero → Settings → Advanced → Config Editor → click through the warning → search `localAPI` → set `extensions.zotero.httpServer.localAPI.enabled` to `true`.
3. **(Optional) Target a specific collection** rather than My Library root:
   - Create/select the collection in Zotero.
   - Find its key: `curl http://127.0.0.1:23119/api/users/0/collections | python3 -m json.tool`
   - `export ZOTERO_COLLECTION_KEY=<the key>`
4. **Point at your remote**: `export RCLONE_REMOTE=dropbox:` (defaults to `dropbox:` if unset).

The first write call will prompt Zotero for a one-time local API key (`POST /api/local/authorize`) and cache it in `.zotero_api_key.json` next to the scripts — **don't commit that file** (already gitignored).

## Usage

```bash
# Bulk-import every document-type file across the whole remote
python3 zotero_dropbox_import.py

# Find archives (.zip/.tar/.tar.gz/.tgz) anywhere on the remote, extract them,
# and import any document-type files found inside
python3 process_archives.py

# Sweep anything that failed, enriching metadata via Open Library / Google
# Books (using the filename as a hint) instead of just re-trying blindly
python3 remediate_failed.py
```

All three are **resumable** — progress lives in JSONL log files (`zotero_import_log.jsonl`, `zotero_archive_log.jsonl`), and re-running skips anything already marked `"ok"`. Safe to kill and restart at any point.

### Handling huge folders

Full machine backups or stale cloud-drive mirrors can be large enough that even *listing* the folder's contents times out, before any document filtering happens. Both `zotero_dropbox_import.py` and `process_archives.py` handle this by recursively subdividing: on a listing timeout, they fall back to listing just the folder's direct subfolders and recurse into each independently, going deeper only where needed (capped at a depth limit so it can't spin forever). Folders still too large at the depth cap are logged by name rather than silently dropped.

### Disk-space safety (archives)

`process_archives.py` processes one archive at a time: checks compressed size against free disk space before downloading, checks total *uncompressed* size against free disk space before extracting, and deletes both the downloaded archive and the extracted tree before moving to the next one. Peak extra disk usage is bounded by roughly one archive's uncompressed size — never the sum of all archives.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `RCLONE_REMOTE` | `dropbox:` | Which rclone remote to scan |
| `ZOTERO_COLLECTION_KEY` | *(unset = My Library root)* | Collection to file new items into |
| `EXCLUDE_TOP_FOLDERS` | `Zotero` | Comma-separated top-level folder names to skip entirely |
| `ZOTERO_STAGING_DIR` | `./stage` | Scratch dir for downloads (cleaned up per-file) |
| `ZOTERO_IMPORT_LOG` | `./zotero_import_log.jsonl` | Per-file resume log |
| `ZOTERO_ARCHIVE_LOG` | `./zotero_archive_log.jsonl` | Per-archive resume log |
| `ZOTERO_DISK_SAFETY_MARGIN_GB` | `5` | Minimum free disk space to always keep |

Supported document extensions (edit `ALLOWED_EXTENSIONS` in `zotero_dropbox_import.py` to change): `.pdf .doc .docx .ppt .pptx .epub .djvu .mobi .chm .ris`

## Files

- `zotero_crud.py` — thin client for Zotero's local CRUD API (auth, item creation, attachment upload)
- `zotero_dropbox_import.py` — main bulk importer
- `process_archives.py` — finds and extracts archives, feeds contents through the same import pipeline
- `remediate_failed.py` — retries failures with internet-sourced metadata enrichment (Open Library / Google Books)

## Known limitations

- No automatic PDF/EPUB metadata recognition (that's a connector-only feature) — items are created with just filename-derived titles unless you run `remediate_failed.py` or use Zotero's own "Retrieve Metadata for PDF" afterward.
- `.rar` / `.7z` archives are logged as unsupported unless you install `unrar`/`7z` and extend `process_archives.py`'s `extract_archive()`.
- Nested archives (a zip inside a zip) are logged but not recursively expanded.
