# research-toolkit

A methodology and reference implementation for turning a large, unsorted pile of documents (thousands of PDFs
and ebooks scattered across cloud storage, buried inside old `.zip` archives) into a catalogued, full-text
searchable research library. It discovers files with `rclone`, imports them into Zotero through its CRUD API,
builds a DuckDB catalogue, converts every document to Markdown for qmd search,
and derives daily "insights" reports. It contains no documents or content from any specific collection.

> Status: maintained (runs daily on the author's Mac via launchd). Personal tooling: several scripts default to
> the author's absolute paths (see Configuration). New here? Read
> [How to use this repo with AI](HOW_TO_USE_WITH_AI.md).

## What it does
- Bulk-imports document files (`pdf doc docx ppt pptx epub djvu mobi chm ris`) from an rclone remote into Zotero
  with filename-derived metadata (`zotero_dropbox_import.py`).
- Finds `.zip/.tar/.tar.gz/.tgz` archives, extracts one at a time and imports their documents (`process_archives.py`).
- Retries failures while enriching metadata from Open Library / Google Books (`remediate_failed.py`).
- Tags, de-duplicates and files items into topic collections (`categorize.py`, `dedupe_items.py`,
  `organize_topic_collections.py`, `smart_classify.py`).
- Builds `document_catalog.duckdb` and classifies every title by subject (`build_duckdb_catalog.py`, `topic_classify.py`).
- Converts the catalogue to Markdown, no LLM calls (`pdf_to_markdown.py`), and produces corpus insights
  (`insights/`, see `insights/README.md`).

## Quick start
```bash
git clone https://github.com/herrrickshaw/research-toolkit.git
cd research-toolkit
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt        # duckdb, pymupdf, python-docx, python-pptx, openpyxl, pytest, ruff
pytest -q                                  # offline tests, no Zotero or cloud needed
```
Running the pipeline needs an rclone remote (`rclone config`) and Zotero with its local API enabled
(Zotero > Settings > Advanced > Config Editor > `extensions.zotero.httpServer.localAPI.enabled` = `true`).
Then: `python3 zotero_dropbox_import.py`.

## How it works
Six principles drive the design:
1. **Discover before you process.** List extensions, sizes and folder structure first; treat plain folders,
   machine backups and archives differently.
2. **Recursive subdivision** for trees too large to list in one call: on timeout list only direct subfolders and
   recurse, up to a depth cap.
3. **Everything is resumable.** Each unit of work is appended to a JSONL log (or a DuckDB table) as it
   succeeds or fails; rerunning skips what is done.
4. **Archives are documents you haven't seen yet.** One archive at a time (download, check disk, extract, scan,
   delete), so peak disk use is about one archive.
5. **Prefer a real CRUD API over a one-shot "save" endpoint** for bulk work (Zotero's connector API returned bare
   `500`s after a few hundred sequential calls).
6. **Enrich metadata from what you have**, using free keyless lookups (Open Library, Google Books).

| Stage | Module | Output |
|---|---|---|
| Zotero client (local or cloud mode) | `zotero_crud.py` | items, attachments, tags |
| Bulk import | `zotero_dropbox_import.py` | Zotero items, `zotero_import_log.jsonl` |
| Archives | `process_archives.py` | `zotero_archive_log.jsonl` |
| Failure sweep + enrichment | `remediate_failed.py` | repaired items |
| Tagging | `categorize.py`, `backfill_tags.py` | `category:/topic:/source:/filetype:/year:` tags |
| Cleanup | `dedupe_items.py`, `organize_topic_collections.py`, `smart_classify.py` | merged duplicates, collections, LLM topic tags |
| Catalogue | `build_duckdb_catalog.py`, `topic_classify.py` | `document_catalog.duckdb` (`documents`, `auto_topic`) |
| Full text | `pdf_to_markdown.py` (`daily_pdf_to_md.sh`, `full_backfill_driver.sh`) | `md_corpus/*.md`, `fulltext_md` table, cloud copy |
| Search backup | `qmd_cloud_backup.sh`, `qmd_cloud_restore.sh` | `dropbox:qmd_cloud_backup/` |
| Insights | `insights/build_insights.py` | `research_insights.duckdb`, `report_latest.md` |

### Full-text extraction (`pdf_to_markdown.py`)
```bash
python3 pdf_to_markdown.py            # one capped batch (what the daily job runs)
python3 pdf_to_markdown.py --status   # progress summary, no side effects
```
Per filetype, fastest path first: pdf/epub via PyMuPDF, docx/pptx/xlsx via python-docx/python-pptx/openpyxl,
legacy doc/ppt/xls in one batched `soffice --headless --convert-to` call, everything else via a `markitdown`
subprocess. Source bytes come from the rclone remotes (not the local Finder mounts, which lack most folders), in
one `rclone copy --files-from` per source; each original plus its `.md` is copied back under
`<remote>:research_md_corpus/`. Batching took a 300-doc batch from about 52 min to about 5 min. No OCR:
scanned pages extract as empty text and `--status` reports the near-empty fraction.
Runs daily at 05:30 (`com.user.pdftomarkdown.plist` -> `daily_pdf_to_md.sh`) with absolute tool paths because
launchd's PATH lacks Homebrew.

### Cloud backup of the qmd index
`md_corpus/` (~5 GB) and qmd's SQLite index (~9 GB) stay local because qmd needs a real local path and SQLite
must not live on a network filesystem. `qmd_cloud_backup.sh` (daily 22:45) copies both to Dropbox using
`sqlite3 .backup` for a consistent snapshot; `qmd_cloud_restore.sh [--force]` restores and refuses to overwrite a
newer local index without `--force`.

## Repository layout
| Path | Purpose |
|---|---|
| top-level `*.py`, `*.sh` | pipeline stages (table above); script names and flags are referenced by launchd jobs |
| `insights/` | daily insights builder, privacy filter, runner |
| `scripts/` | `check_*_done.py` progress checkers |
| `tests/` | offline pytest suite; `tests/golden/` holds outputs captured from the pre-refactor code |
| `GATES.md`, `HOW_TO_USE_WITH_AI.md`, `CLAUDE.md` | gate checklist, AI usage guide, assistant guidance |

Gitignored because they hold real personal document titles/text: `document_catalog.duckdb`, `md_corpus/`,
`pdf2md_stage/`, `*.jsonl`, `*.log`, `.zotero_api_key.json`, `topic_sweep_*.md`.

## Data sources
| Source | What is taken | Access route | Caveat |
|---|---|---|---|
| Cloud storage (Dropbox, Google Drive) | document bytes and listings | `rclone` | your own account |
| Zotero | library items/attachments | local API `127.0.0.1:23119` or `api.zotero.org` | local API off by default |
| Open Library, Google Books | bibliographic metadata | free read-only APIs | rate limits |
| Groq | title-only topic classification (`smart_classify.py`) | API key read from a local credentials file | titles leave the machine |

## Configuration
| Variable | Default | Purpose |
|---|---|---|
| `RCLONE_REMOTE` | `dropbox:` | remote to scan |
| `ZOTERO_MODE` | `local` | `cloud` uses `api.zotero.org` (needs `ZOTERO_API_KEY`, `ZOTERO_LIBRARY_PATH`) |
| `ZOTERO_COLLECTION_KEY` | unset | collection for new items (unset = library root) |
| `EXCLUDE_TOP_FOLDERS` | `Zotero` | top-level folders to skip |
| `ZOTERO_STAGING_DIR` / `ZOTERO_IMPORT_LOG` / `ZOTERO_ARCHIVE_LOG` | `./stage`, `./zotero_import_log.jsonl`, `./zotero_archive_log.jsonl` | scratch and resume logs |
| `ZOTERO_DISK_SAFETY_MARGIN_GB` | `5` | free space always kept |
| `PDF2MD_*` | see top of `pdf_to_markdown.py` | batch caps, timeouts, binary paths; `PDF2MD_REPO_DIR` relocates all outputs |
| `TOPIC_CLASSIFY_DB`, `CLASSIFY_CREDENTIALS_FILE` | author's paths | catalogue DB and Groq credentials file |

Secrets (`GROQ_API_KEY`, Zotero keys) are read from the environment or a local file and never committed.

## Testing
```bash
pytest -q          # 38 tests; fake rclone/soffice/markitdown, in-process fake Zotero server, no network
ruff check .
```

## Limitations and known issues
- No automatic PDF/EPUB metadata recognition (that is Zotero's browser connector, not its CRUD API); items get
  filename-derived titles unless you run `remediate_failed.py` or Zotero's "Retrieve Metadata".
- `.rar` / `.7z` archives are logged as unsupported; nested archives are logged but not expanded.
- No OCR: scanned documents yield empty Markdown.
- Several scripts hard-code the author's home paths as defaults (`/Users/umashankar/...`).
- `topic_classify.py` is a keyword-on-title classifier; roughly 90% of the author's catalogue is generic backup
  mass with no recoverable subject, so the unclassified rate is a ceiling, not a bug.

## Licence
No licence file yet; all rights reserved.
