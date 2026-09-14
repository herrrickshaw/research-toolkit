# research-toolkit

A methodology and reference implementation for turning a large, unsorted pile of documents — thousands of PDFs and ebooks scattered across cloud storage, buried inside old `.zip` archives, mixed in with everything else — into a properly cataloged research library, without babysitting it file by file.

This is not a research corpus and contains no documents, filenames, or content from any specific collection. It's the pipeline: how to discover, extract, and catalog documents at a scale where naive "just loop over the files" scripts fall over.

## The methodology

Processing tens of thousands of files across an unpredictable folder structure surfaces problems that don't show up at small scale. The approach here:

1. **Discover before you process.** List what exists (extensions, sizes, folder structure) before committing to a strategy — some folders will be plain documents, some will be full machine backups, some will be archives worth opening. Treat these differently rather than one blind recursive walk.

2. **Recursive subdivision for trees too large to list in one call.** A single "list this folder recursively" request can itself time out on a big enough tree — before you've even started filtering. The fix: on timeout, list only the folder's *direct* subfolders (fast) and recurse into each independently, going deeper only where needed, capped at a depth limit. This turns "one huge folder that hangs forever" into "many small folders that each complete quickly."

3. **Everything is resumable.** Every unit of work (one file, one archive) gets logged to an append-only JSONL file the moment it succeeds or fails. A crash, a network drop, or a deliberate `Ctrl-C` costs nothing — rerunning the same command skips everything already done and only retries what wasn't. This matters enormously at scale: a run against tens of thousands of files will hit *something* transient (a timeout, a dropped connection) and needs to survive it without starting over.

4. **Archives are documents you haven't seen yet.** A `.zip` full of PDFs is functionally the same as a folder full of PDFs — it just needs one extra step. Process one archive at a time (download → check real uncompressed size against free disk space → extract → scan contents → delete both the archive and the extraction), so peak disk usage never exceeds roughly one archive's size, regardless of how many archives exist in total.

5. **A one-shot "save" API degrades under bulk load; a real CRUD API doesn't.** Many local apps expose a browser-extension-style "save this one item" endpoint that works fine for occasional use but accumulates internal state under sustained load (here: Zotero's connector API started returning bare `500`s after a few hundred sequential calls). Where a real REST-style CRUD API exists underneath, prefer it for bulk work — it's built for repeated, independent calls rather than one-off interactive actions.

6. **Enrich metadata from what you have, using free public lookups.** A raw filename often already encodes a title, author, and year (`Author - Title (Year).pdf`). Parse what's there, then confirm/enrich it against a free bibliographic API (Open Library, Google Books) — no scraping, no keys required for read-only lookups — so items land in the library with real metadata instead of a bare filename.

## Reference implementation (Zotero)

The scripts here implement this methodology concretely for importing documents from a cloud storage remote (via [rclone](https://rclone.org)) into a local [Zotero](https://www.zotero.org) library. Swap in a different remote or a different destination API and the same structure applies.

### Setup

1. **rclone remote** for your cloud storage: `rclone config` (see [rclone docs](https://rclone.org/docs/)).
2. **Enable Zotero's local API** — it's off by default:
   Zotero → Settings → Advanced → Config Editor → click through the warning → search `localAPI` → set `extensions.zotero.httpServer.localAPI.enabled` to `true`.
3. **(Optional) Target a specific collection** rather than My Library root:
   - Create/select the collection in Zotero.
   - Find its key: `curl http://127.0.0.1:23119/api/users/0/collections | python3 -m json.tool`
   - `export ZOTERO_COLLECTION_KEY=<the key>`
4. **Point at your remote**: `export RCLONE_REMOTE=dropbox:` (defaults to `dropbox:` if unset).

The first write call prompts Zotero for a one-time local API key (`POST /api/local/authorize`) and caches it in `.zotero_api_key.json` next to the scripts — gitignored by default; don't commit it.

### Usage

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

All three are resumable via JSONL log files (`zotero_import_log.jsonl`, `zotero_archive_log.jsonl`) — safe to kill and restart at any point.

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
- `zotero_dropbox_import.py` — main bulk importer, implements points 1-3 above
- `process_archives.py` — implements point 4 (archive discovery/extraction)
- `remediate_failed.py` — implements point 6 (failure retry + metadata enrichment)

## Known limitations

- No automatic PDF/EPUB metadata recognition (that's specific to Zotero's browser-connector, not its CRUD API) — items get filename-derived titles unless you run `remediate_failed.py` or use Zotero's own "Retrieve Metadata for PDF" afterward.
- `.rar` / `.7z` archives are logged as unsupported unless you install `unrar`/`7z` and extend `process_archives.py`'s `extract_archive()`.
- Nested archives (a zip inside a zip) are logged but not recursively expanded.
