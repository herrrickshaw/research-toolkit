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

## Full-text extraction (pdf_to_markdown.py)

`build_duckdb_catalog.py` gives metadata only. `pdf_to_markdown.py` fills the
gap: converts every catalogued document (pdf, epub, docx, pptx, xlsx, plus
legacy doc/ppt and a long tail — see below) to a standalone `.md` file, no
LLM/agent calls (costs no tokens to run), so the corpus becomes full-text
searchable via `qmd search` (the `research` collection in
`~/.config/qmd/index.yml`), not just browsable by filename. Despite the
filename, it now covers the whole catalog, not just PDFs — kept as-is since
PDFs are still the large majority (~12k of ~13.7k documents).

```bash
python3 pdf_to_markdown.py           # one capped batch (what the daily job runs)
python3 pdf_to_markdown.py --status  # progress summary, no side effects
```

Extraction is dispatched per filetype, fastest path first:
- **pdf, epub** → in-process PyMuPDF (`fitz`) — ~0.04-0.4s/doc.
- **docx, pptx, xlsx** → in-process `python-docx`/`python-pptx`/`openpyxl`.
- **doc, ppt** (legacy binary Office — markitdown has no converter for
  these at all) → one batched `soffice --headless --convert-to txt` call
  per run covering every doc/ppt in the batch at once, not one subprocess
  per file.
- **everything else** (mobi, djvu, chm, ris, html, malformed filetype
  values) → a `markitdown` subprocess per file, the slow-path fallback;
  small enough by count (~2.5% of the catalog) that it doesn't dominate.
- No OCR: image-only/scanned pages extract as empty text (`--status` shows
  the near-empty fraction so a future OCR pass could target just those).

Key points (see the script's own docstring for the full rationale):
- Source bytes come from the `dropbox:`/`googledrive:` rclone remotes, not
  the local Finder/CloudStorage mounts — most catalogued folders aren't
  actually present locally (selective sync), even though they're in the
  cloud account that was originally scanned.
- **Batched I/O**: one `rclone copy --files-from=<list>` per source to
  bulk-fetch a whole batch, then one bulk `rclone copy` per source to
  upload it all back — not one `copyto` per file. This (plus in-process
  extraction over markitdown subprocesses) took a 300-doc batch from ~52min
  to ~5min.
- Every conversion is copied BACK to the cloud (original file + its new
  `.md`) under `<same remote>:research_md_corpus/` — Create-only via a real
  rclone/API call ("CRUD methodology"), never touching the source file.
- Resumable via a `fulltext_md` table in `document_catalog.duckdb`, keyed
  by `item_key` (md5 is populated for too few rows to use as the key).
- Capped per run (`PDF2MD_MAX_DOCS_PER_RUN`, default 1000;
  `PDF2MD_MAX_MB_PER_RUN`, default 5000) and disk-safety-margin-checked.
- Runs daily at 05:30 via `launchd` (`com.user.pdftomarkdown.plist` →
  `daily_pdf_to_md.sh`); `full_backfill_driver.sh` loops it continuously
  in the background for an initial bulk catch-up, pausing the daily job
  while it runs and re-enabling it on completion. Logged to
  `daily_pdf_to_md.log`/`.err` and `full_backfill_run.log`. Absolute paths
  to `rclone`/`markitdown`/`soffice` are hardcoded rather than relying on
  `$PATH`, since launchd's minimal PATH doesn't include Homebrew or
  `~/.local/bin`.
- Output (`md_corpus/`) is gitignored — real extracted document text, same
  reason `document_catalog.duckdb` itself is excluded.

## Cloud backup for the qmd search index (qmd_cloud_backup.sh / qmd_cloud_restore.sh)

`md_corpus/` (5.2GB) and qmd's own search index (`~/.cache/qmd/index.sqlite`,
~9GB) are both local-only by default — qmd has no live-cloud-read mode, so
they can't just be moved to Dropbox/Google Drive and mounted: qmd needs
`md_corpus/` on a real local path to scan/index it, and its SQLite index is
actively queried (several `qmd mcp` server processes hold it open), which
SQLite's own docs warn against doing over a network filesystem (corruption
risk + very poor performance from constant small random reads). This Mac
also has no macFUSE installed, so `rclone mount` isn't even available
without the user personally approving a kernel extension.

Instead: a daily backup (`com.user.qmdcloudbackup.plist`, 22:45) copies both
to `dropbox:qmd_cloud_backup/` — the index via `sqlite3 .backup` first (a
consistent snapshot, safe even while qmd mcp is running, not a raw copy of
a possibly-mid-write file) — and `qmd_cloud_restore.sh` pulls everything
back down (refuses to overwrite an existing local index/corpus unless
`--force`, since a blind restore over a newer local index would lose
anything indexed since the last backup). This gets the actual benefit
asked for — cloud-backed, portable to a new machine, survives local disk
loss — without breaking live search or risking DB corruption.

```bash
./qmd_cloud_backup.sh    # what the daily 22:45 job runs
./qmd_cloud_restore.sh --force   # rebuild local md_corpus/ + index from the cloud backup
```

## Known limitations

- No automatic PDF/EPUB metadata recognition (that's specific to Zotero's browser-connector, not its CRUD API) — items get filename-derived titles unless you run `remediate_failed.py` or use Zotero's own "Retrieve Metadata for PDF" afterward.
- `.rar` / `.7z` archives are logged as unsupported unless you install `unrar`/`7z` and extend `process_archives.py`'s `extract_archive()`.
- Nested archives (a zip inside a zip) are logged but not recursively expanded.
