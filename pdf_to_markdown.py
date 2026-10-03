#!/usr/bin/env python3
"""
Full-text extraction pass over the document_catalog: converts each catalogued
PDF to a standalone .md file (no LLM/agent involvement, so this costs zero
tokens to run — a plain script meant to run unattended via launchd, see
daily_pdf_to_md.sh / com.user.pdftomarkdown.plist).

Why this exists: build_duckdb_catalog.py gives metadata only (title, path,
category, topic, dedup) for ~16.8k documents pulled from Dropbox/Google
Drive into Zotero. This pass fills the full-text gap so the corpus is
queryable with qmd (see the 'research' collection in ~/.config/qmd/index.yml).

Methodology, consistent with the rest of this pipeline:
  1. One row per catalogued item (documents.item_key, its real primary key),
     restricted to is_duplicate=false. md5 is populated for too few rows
     (~32 of 12,145) to use as the dedup/resume key, so item_key is used.
  2. Bytes come from the `dropbox:`/`googledrive:` rclone remotes
     zotero_dropbox_import.py originally scanned — not the local
     ~/Dropbox / CloudStorage/GoogleDrive-* Finder mounts, which are
     missing most catalogued folders (selective sync) even though rclone
     (hitting the cloud API directly) sees them fine.
  3. Zotero itself is NOT a source: the live Zotero library on this Mac (63
     items) is not the library document_catalog.duckdb was built from.
  4. A path containing "!" denotes a file extracted from an archive during
     the original import and deleted afterward per process_archives.py's
     own cleanup — nothing to fetch, marked 'archive_not_persisted'.
  5. Every successful conversion is copied BACK to the cloud (the PDF plus
     its new .md sibling) under <same remote as source>:research_md_corpus/
     — the "CRUD methodology" from this repo's README point 5 (a real
     Create call against the Dropbox/Google Drive API, not a one-shot
     upload hack). Create-only: the source file at its original
     catalog_path is never touched. Tracked via cloud_status so a failed
     copy-back is retried independently of re-converting.
  6. BATCHED I/O, not one-file-at-a-time (added after the v1 architecture
     turned out to need ~30+ hours for the full backfill): each run does
     ONE `rclone copy --files-from=<list> --transfers N` per source to
     bulk-fetch the whole batch, converts everything in-process, then ONE
     bulk `rclone copy` per source to upload the whole batch back. This
     collapses ~3 subprocess calls/doc into ~4 calls per *batch* (of up to
     PDF2MD_MAX_DOCS_PER_RUN docs), each internally parallelized by rclone.
  7. Extraction is in-process PyMuPDF (`fitz.get_text()`), not a markitdown
     subprocess: benchmarked at ~0.04-0.4s/doc versus markitdown's ~4-5s/doc
     (mostly Python-interpreter-per-file startup cost), with PyMuPDF's own
     import cost (~0.5s) paid once per run instead of once per file. This
     is plain text (no markdown headers/tables), which is the right
     trade-off here: the corpus exists to be full-text *searchable* via
     qmd, not to be read as formatted documents.
     Caveat: get_text() returns nothing for image-only/scanned pages (no
     OCR). --status reports the near-empty (<200 char) fraction of
     converted docs so scanned documents worth a separate OCR pass can be
     identified later — this run does NOT do OCR itself.

Usage:
  python3 pdf_to_markdown.py                 # one capped batch (for cron/launchd)
  python3 pdf_to_markdown.py --max-docs 20    # small manual test run
  python3 pdf_to_markdown.py --status         # print progress summary, do nothing else
"""
import argparse
import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import duckdb
import docx as python_docx
import fitz  # PyMuPDF
import openpyxl
import pptx as python_pptx

# MuPDF prints malformed-CSS/XObject warnings straight to stderr for a lot
# of real-world epub/pdf files — cosmetic noise (extraction still succeeds),
# but it floods the log at thousands-of-docs scale. Silence it.
fitz.TOOLS.mupdf_display_errors(False)
fitz.TOOLS.mupdf_display_warnings(False)
# Same for openpyxl on real-world xlsx files with unsupported extensions
# (data validation, sparklines, ...) — cosmetic, extraction still works.
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

REPO_DIR = Path("/Users/umashankar/research-toolkit")
DB_PATH = REPO_DIR / "document_catalog.duckdb"
MD_OUTPUT_DIR = REPO_DIR / "md_corpus"
STAGE_DIR = REPO_DIR / "pdf2md_stage"
FETCH_DIR = STAGE_DIR / "fetch"
UPLOAD_DIR = STAGE_DIR / "upload"

RCLONE_REMOTE_BY_SOURCE = {"dropbox": "dropbox:", "googledrive": "googledrive:"}
CLOUD_DEST_FOLDER = "research_md_corpus"  # created under the SAME remote each item came from

# Absolute paths, not bare command names: launchd runs jobs with a minimal
# PATH that doesn't include Homebrew or ~/.local/bin (documented elsewhere
# in this account's automation as the cqlsh/rclone-under-launchd issue).
RCLONE_BIN = os.environ.get("PDF2MD_RCLONE_BIN", "/opt/homebrew/bin/rclone")
MARKITDOWN_BIN = os.environ.get("PDF2MD_MARKITDOWN_BIN", "/Users/umashankar/.local/bin/markitdown")
MARKITDOWN_TIMEOUT_S = int(os.environ.get("PDF2MD_MARKITDOWN_TIMEOUT_S", "180"))
SOFFICE_BIN = os.environ.get("PDF2MD_SOFFICE_BIN", "/opt/homebrew/bin/soffice")
SOFFICE_TIMEOUT_S = int(os.environ.get("PDF2MD_SOFFICE_TIMEOUT_S", "900"))

# Fast, in-process extraction covers the bulk of the catalog by volume
# (pdf, epub, docx, pptx). markitdown genuinely has no converter at all for
# legacy binary .doc/.ppt (confirmed: UnsupportedFormatException), so those
# go to a batched LibreOffice (soffice) headless pass instead — one
# subprocess call converts the WHOLE batch's doc/ppt/xls files together
# (soffice accepts multiple inputs per invocation), amortizing its ~5s
# fixed startup cost across the batch instead of paying it per file.
# Everything else genuinely without a good open-source path here (mobi,
# djvu, chm, ris; html; malformed/garbage filetype values from a handful of
# source paths that were really URLs) falls back to a markitdown subprocess
# per file — slower, but small enough by count that it never dominates.
FAST_PATH_FILETYPES = {"pdf", "epub", "docx", "pptx", "xlsx"}
SOFFICE_FILETYPES = {"doc", "ppt", "xls"}  # openpyxl genuinely cannot read legacy .xls

MAX_DOCS_PER_RUN = int(os.environ.get("PDF2MD_MAX_DOCS_PER_RUN", "1000"))
MAX_MB_PER_RUN = float(os.environ.get("PDF2MD_MAX_MB_PER_RUN", "5000"))
MAX_SINGLE_FILE_MB = float(os.environ.get("PDF2MD_MAX_SINGLE_FILE_MB", "300"))
DISK_SAFETY_MARGIN_GB = float(os.environ.get("PDF2MD_DISK_SAFETY_MARGIN_GB", "5"))
RCLONE_TIMEOUT_S = int(os.environ.get("PDF2MD_RCLONE_TIMEOUT_S", "1800"))
RCLONE_TRANSFERS = os.environ.get("PDF2MD_RCLONE_TRANSFERS", "16")
RCLONE_CHECKERS = os.environ.get("PDF2MD_RCLONE_CHECKERS", "8")


def init_table(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS fulltext_md (
            item_key VARCHAR PRIMARY KEY,
            md5 VARCHAR,
            title VARCHAR,
            source VARCHAR,
            catalog_path VARCHAR,
            md_path VARCHAR,
            status VARCHAR,        -- ok | archive_not_persisted | fetch_failed | convert_failed | too_large | unsupported_source
            chars BIGINT,
            source_mb DOUBLE,
            converted_at TIMESTAMP,
            error VARCHAR,
            cloud_status VARCHAR,   -- ok | failed (only meaningful when status='ok')
            cloud_pdf_path VARCHAR,
            cloud_md_path VARCHAR
        )
    """)
    existing_cols = {r[0] for r in con.execute("describe fulltext_md").fetchall()}
    for col in ("cloud_status", "cloud_pdf_path", "cloud_md_path"):
        if col not in existing_cols:
            con.execute(f"ALTER TABLE fulltext_md ADD COLUMN {col} VARCHAR")


def free_disk_gb():
    return shutil.disk_usage(REPO_DIR).free / (1024 ** 3)


KNOWN_EXTENSIONS = ("pdf", "docx", "pptx", "xlsx", "doc", "ppt", "xls", "epub",
                     "mobi", "djvu", "chm", "ris", "html", "htm")


def safe_title_for(title):
    title = title or "untitled"
    low = title.lower()
    for ext in KNOWN_EXTENSIONS:
        if low.endswith("." + ext):
            title = title[: -(len(ext) + 1)]
            break
    return "".join(c if c.isalnum() or c in " -_." else "_" for c in title)[:80]


def ext_for(catalog_path):
    """The real source extension, sanitized, for naming cloud/upload copies
    — never guessed from the (sometimes garbage) filetype column."""
    suffix = Path(catalog_path).suffix.lstrip(".")
    safe = "".join(c for c in suffix if c.isalnum())[:10]
    return f".{safe}" if safe else ".bin"


def md_output_path(item_key, title):
    return MD_OUTPUT_DIR / f"{item_key}_{safe_title_for(title)}.md"


def extract_text_fitz(path):
    """pdf and epub — fitz opens both the same way. See docstring point 7:
    ~0.04-0.4s/doc in-process versus ~4-5s/doc via a markitdown subprocess."""
    doc = fitz.open(path)
    try:
        if doc.needs_pass:
            return None, "password-protected document"
        return "\n\n".join(page.get_text() for page in doc), None
    finally:
        doc.close()


def extract_text_docx(path):
    d = python_docx.Document(str(path))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return "\n\n".join(parts), None


def extract_text_pptx(path):
    prs = python_pptx.Presentation(str(path))
    parts = []
    for i, slide in enumerate(prs.slides, 1):
        lines = [f"## Slide {i}"]
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in para.runs)
                    if line.strip():
                        lines.append(line)
            if shape.has_table:
                for row in shape.table.rows:
                    lines.append(" | ".join(cell.text for cell in row.cells))
        if len(lines) > 1:
            parts.append("\n".join(lines))
    return "\n\n".join(parts), None


def extract_text_xlsx(path):
    """No .xlsx currently exists in the catalog (the original import's
    ALLOWED_EXTENSIONS never included it) — kept for completeness/future
    catalog extensions rather than anything this run will actually hit."""
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        parts = []
        for ws in wb.worksheets:
            lines = [f"## Sheet: {ws.title}"]
            for row in ws.iter_rows(values_only=True):
                if any(c is not None for c in row):
                    lines.append(" | ".join("" if c is None else str(c) for c in row))
            if len(lines) > 1:
                parts.append("\n".join(lines))
        return "\n\n".join(parts), None
    finally:
        wb.close()


def extract_text_markitdown_fallback(path):
    """Slow-path subprocess fallback for filetypes without a fast in-process
    library here: doc, ppt (legacy binary Office), mobi, djvu, chm, ris,
    html, unknown, and any malformed filetype value."""
    try:
        result = subprocess.run(
            [MARKITDOWN_BIN, str(path)],
            capture_output=True, text=True, timeout=MARKITDOWN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None, f"markitdown timeout after {MARKITDOWN_TIMEOUT_S}s"
    if result.returncode != 0:
        return None, (result.stderr or "markitdown failed").strip()[-500:]
    return result.stdout, None


def extract_text(path, filetype):
    ft = (filetype or "").lower()
    try:
        if ft in ("pdf", "epub"):
            return extract_text_fitz(path)
        if ft == "docx":
            return extract_text_docx(path)
        if ft == "pptx":
            return extract_text_pptx(path)
        if ft == "xlsx":
            return extract_text_xlsx(path)
    except Exception as e:  # noqa: BLE001 — any real-world file can be malformed
        return None, f"{ft} extraction error: {str(e)[-400:]}"
    return extract_text_markitdown_fallback(path)


# soffice needs the export filter that matches the document's application
# (Writer vs Calc) — using the Writer "txt:Text" filter against a Calc
# document (.xls) silently produces empty output for the WHOLE batch, not
# just that file (confirmed: a mixed run failed 143/143 xls this way).
SOFFICE_FILTER_BY_FILETYPE = {
    "doc": ("txt:Text", ".txt"),
    "ppt": ("txt:Text", ".txt"),
    "xls": ("csv:Text - txt - csv (StarCalc)", ".csv"),
}


def convert_batch_soffice(items):
    """items: list of (item_key, staged_path, real_extension, filetype).
    One `soffice --convert-to` call per FILTER GROUP (doc/ppt share one,
    xls needs another) rather than one call per file — soffice accepts
    multiple inputs per invocation, amortizing its ~5s startup cost across
    everything in that group. Returns {item_key: (text_or_None, error_or_None)}."""
    if not items:
        return {}
    results = {}
    by_filter = {}
    for item_key, staged_path, ext, filetype in items:
        soffice_filter, out_ext = SOFFICE_FILTER_BY_FILETYPE.get(filetype, ("txt:Text", ".txt"))
        by_filter.setdefault(soffice_filter, []).append((item_key, staged_path, ext, out_ext))

    for soffice_filter, group in by_filter.items():
        batch_in = STAGE_DIR / "soffice_in"
        batch_out = STAGE_DIR / "soffice_out"
        profile_dir = STAGE_DIR / "soffice_profile"
        batch_in.mkdir(parents=True, exist_ok=True)
        batch_out.mkdir(parents=True, exist_ok=True)
        input_paths = []
        for item_key, staged_path, ext, _out_ext in group:
            dest = batch_in / f"{item_key}{ext}"
            shutil.copy2(staged_path, dest)
            input_paths.append(str(dest))
        try:
            subprocess.run(
                [SOFFICE_BIN, "--headless", "--norestore",
                 f"-env:UserInstallation=file://{profile_dir}",
                 "--convert-to", soffice_filter, "--outdir", str(batch_out)] + input_paths,
                capture_output=True, text=True, timeout=SOFFICE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            pass  # partial output, if any, is still picked up below
        for item_key, _staged_path, _ext, out_ext in group:
            out_file = batch_out / f"{item_key}{out_ext}"
            if out_file.exists():
                results[item_key] = (out_file.read_text(errors="replace"), None)
            else:
                results[item_key] = (None, "soffice produced no output for this file")
        shutil.rmtree(batch_in, ignore_errors=True)
        shutil.rmtree(batch_out, ignore_errors=True)
        shutil.rmtree(profile_dir, ignore_errors=True)
    return results


def bulk_fetch(items_by_source):
    """One `rclone copy --files-from` per source instead of one `copyto`
    per file — see docstring point 6. Returns {source: n_files_listed}."""
    counts = {}
    for source, items in items_by_source.items():
        remote = RCLONE_REMOTE_BY_SOURCE.get(source)
        if remote is None or not items:
            continue
        dest = FETCH_DIR / source
        dest.mkdir(parents=True, exist_ok=True)
        listfile = STAGE_DIR / f"fetch_list_{source}.txt"
        listfile.write_text("\n".join(path for _key, path in items))
        try:
            subprocess.run(
                [RCLONE_BIN, "copy", remote, str(dest),
                 "--files-from", str(listfile),
                 "--transfers", RCLONE_TRANSFERS, "--checkers", RCLONE_CHECKERS,
                 "--fast-list", "--ignore-errors"],
                capture_output=True, text=True, timeout=RCLONE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            # Same class of bug as bulk_upload's fix: was crashing the
            # whole script uncaught. Whatever rclone fetched before the
            # timeout is still on disk — the per-item `staged.exists()`
            # check in run() already handles a partial fetch correctly,
            # marking anything missing as fetch_failed (self-heals next run).
            pass
        counts[source] = len(items)
    return counts


def bulk_upload(sources_present):
    """One `rclone copy` per source to push the whole batch's upload/
    folder (already named/laid out exactly as it should land) back to
    <remote>:research_md_corpus/ in one parallelized call."""
    results = {}
    for source in sources_present:
        remote = RCLONE_REMOTE_BY_SOURCE.get(source)
        local_dir = UPLOAD_DIR / source
        if remote is None or not local_dir.exists() or not any(local_dir.iterdir()):
            results[source] = True
            continue
        try:
            result = subprocess.run(
                [RCLONE_BIN, "copy", str(local_dir), f"{remote}{CLOUD_DEST_FOLDER}/",
                 "--transfers", RCLONE_TRANSFERS, "--checkers", RCLONE_CHECKERS],
                capture_output=True, text=True, timeout=RCLONE_TIMEOUT_S,
            )
            results[source] = result.returncode == 0
        except subprocess.TimeoutExpired:
            # Was crashing the whole script uncaught (confirmed: cost a
            # 30min timeout wait, twice, before finally succeeding on a
            # 3rd attempt via the driver loop's restart-on-failure) —
            # treat like an ordinary failure instead, self-heals via
            # retry_cloud_failures on the next run same as any other one.
            results[source] = False
    return results


def record(con, item_key, md5, title, source, catalog_path, md_path, status, chars, source_mb, error,
           cloud_status=None, cloud_pdf_path=None, cloud_md_path=None):
    con.execute(
        "insert into fulltext_md values (?,?,?,?,?,?,?,?,?,now(),?,?,?,?) on conflict (item_key) do nothing",
        [item_key, md5, title, source, catalog_path, md_path, status, chars, source_mb, error,
         cloud_status, cloud_pdf_path, cloud_md_path],
    )


def retry_cloud_failures(con, limit=200):
    """Items that converted fine but whose copy-back failed, or never ran
    (cloud_status IS NULL — includes rows from before this step existed),
    grouped and retried via the same bulk_upload path — not one rclone call
    per item."""
    rows = con.execute("""
        select item_key, source, md_path, title
        from fulltext_md where status='ok' and (cloud_status='failed' or cloud_status is null)
        limit ?
    """, [limit]).fetchall()
    if not rows:
        return
    print(f"  retrying cloud copy-back for {len(rows)} previously-failed/unattempted item(s)...")
    # These items' original PDFs were already deleted from stage after their
    # first conversion; only the .md side can be healed without a re-fetch,
    # which is the common case (the catalog PDF copy-back rarely fails and
    # the .md always exists locally) — good enough for a self-heal pass.
    healed = 0
    for source in set(r[1] for r in rows):
        local_dir = UPLOAD_DIR / source
        local_dir.mkdir(parents=True, exist_ok=True)
    for item_key, source, md_path, title in rows:
        if not md_path or not Path(md_path).exists():
            continue
        safe_title = safe_title_for(title)
        shutil.copy2(md_path, UPLOAD_DIR / source / f"{item_key}_{safe_title}.md")
    upload_results = bulk_upload(set(r[1] for r in rows))
    for item_key, source, md_path, title in rows:
        if not md_path or not Path(md_path).exists():
            continue
        if upload_results.get(source):
            safe_title = safe_title_for(title)
            remote = RCLONE_REMOTE_BY_SOURCE[source]
            con.execute(
                "update fulltext_md set cloud_status='ok', cloud_md_path=? where item_key=?",
                [f"{remote}{CLOUD_DEST_FOLDER}/{item_key}_{safe_title}.md", item_key],
            )
            healed += 1
    for source in set(r[1] for r in rows):
        shutil.rmtree(UPLOAD_DIR / source, ignore_errors=True)
    print(f"  ...{healed}/{len(rows)} healed (md re-uploaded; original PDF copy-back skipped — "
          f"already deleted from local stage after first conversion).")


def run(max_docs, max_mb, status_only=False):
    MD_OUTPUT_DIR.mkdir(exist_ok=True)
    STAGE_DIR.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    init_table(con)

    total_pdfs = con.execute(
        "select count(*) from documents where is_duplicate=false"
    ).fetchone()[0]
    done = con.execute("select count(*) from fulltext_md").fetchone()[0]
    ok = con.execute("select count(*) from fulltext_md where status='ok'").fetchone()[0]

    if status_only:
        by_status = con.execute("select status, count(*) from fulltext_md group by 1 order by 2 desc").fetchall()
        by_cloud = con.execute(
            "select cloud_status, count(*) from fulltext_md where status='ok' group by 1 order by 2 desc"
        ).fetchall()
        avg_chars = con.execute("select avg(chars) from fulltext_md where status='ok'").fetchone()[0]
        near_empty = con.execute(
            "select count(*) from fulltext_md where status='ok' and chars < 200"
        ).fetchone()[0]
        print(f"catalog documents (non-duplicate, all filetypes): {total_pdfs}")
        print(f"processed so far:             {done} ({ok} converted)")
        print(f"remaining:                    {total_pdfs - done}")
        for s, c in by_status:
            print(f"  {s}: {c}")
        print("cloud copy-back (of converted items):")
        for s, c in by_cloud:
            print(f"  {s}: {c}")
        if ok:
            print(f"avg chars/doc: {avg_chars:.0f} | near-empty (<200 chars, likely scanned, no OCR run): "
                  f"{near_empty} ({100*near_empty/ok:.1f}%)")
        return

    if free_disk_gb() < DISK_SAFETY_MARGIN_GB:
        print(f"disk safety margin hit (<{DISK_SAFETY_MARGIN_GB} GB free); stopping.")
        con.close()
        return

    retry_cloud_failures(con)

    pending = con.execute("""
        select d.item_key, d.md5, d.title, d.path, d.source, d.filetype
        from documents d
        left join fulltext_md f on f.item_key = d.item_key
        where d.is_duplicate=false and f.item_key is null
        order by d.item_key
        limit ?
    """, [max_docs]).fetchall()

    total_pending = con.execute("""
        select count(*) from documents d
        left join fulltext_md f on f.item_key = d.item_key
        where d.is_duplicate=false and f.item_key is null
    """).fetchone()[0]

    print(f"→ {total_pending} document(s) pending out of {total_pdfs} catalogued "
          f"(processed {done} so far). This batch: {len(pending)} docs.")

    if FETCH_DIR.exists():
        shutil.rmtree(FETCH_DIR, ignore_errors=True)
    if UPLOAD_DIR.exists():
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)

    n_archive_gone = n_unsupported = 0
    to_fetch_by_source = {}
    item_meta = {}
    for item_key, md5, title, path, source, filetype in pending:
        item_meta[item_key] = (md5, title, path, source, filetype)
        if "!" in path:
            record(con, item_key, md5, title, source, path, None, "archive_not_persisted", None, None,
                   "path is inside an archive that was deleted after the original import")
            n_archive_gone += 1
            continue
        if source not in RCLONE_REMOTE_BY_SOURCE:
            record(con, item_key, md5, title, source, path, None, "unsupported_source", None, None,
                   f"no rclone remote for source={source}")
            n_unsupported += 1
            continue
        to_fetch_by_source.setdefault(source, []).append((item_key, path))

    total_to_fetch = sum(len(v) for v in to_fetch_by_source.values())
    print(f"  bulk-fetching {total_to_fetch} file(s) across {len(to_fetch_by_source)} source(s)...")
    bulk_fetch(to_fetch_by_source)

    n_ok = n_fetch_failed = n_convert_failed = n_too_large = n_cloud_failed = 0
    mb_read = 0.0
    sources_uploaded = set()
    soffice_queue = []  # (item_key, staged, ext, source, upload_dir) — converted in one batched pass below

    def finalize_ok(item_key, md5, title, source, path, text, size_mb, staged, upload_dir):
        nonlocal n_ok
        out_path = md_output_path(item_key, title)
        out_path.write_text(text)
        safe_title = safe_title_for(title)
        shutil.copy2(staged, upload_dir / f"{item_key}_{safe_title}{ext_for(path)}")
        shutil.copy2(out_path, upload_dir / f"{item_key}_{safe_title}.md")
        sources_uploaded.add(source)
        record(con, item_key, md5, title, source, path, str(out_path), "ok", len(text), round(size_mb, 2), None)
        n_ok += 1

    for source, items in to_fetch_by_source.items():
        upload_dir = UPLOAD_DIR / source
        upload_dir.mkdir(parents=True, exist_ok=True)
        for item_key, path in items:
            md5, title, _path, _source, filetype = item_meta[item_key]
            staged = FETCH_DIR / source / path
            if not staged.exists():
                record(con, item_key, md5, title, source, path, None, "fetch_failed", None, None,
                       "not present after bulk rclone copy (moved/deleted/renamed since catalog build?)")
                n_fetch_failed += 1
                continue

            size_mb = staged.stat().st_size / (1024 * 1024)
            mb_read += size_mb
            if size_mb > MAX_SINGLE_FILE_MB:
                record(con, item_key, md5, title, source, path, None, "too_large", None, round(size_mb, 2),
                       f"{size_mb:.0f} MB exceeds PDF2MD_MAX_SINGLE_FILE_MB={MAX_SINGLE_FILE_MB}")
                n_too_large += 1
                continue

            if (filetype or "").lower() in SOFFICE_FILETYPES:
                soffice_queue.append((item_key, staged, ext_for(path), source, upload_dir, size_mb,
                                       (filetype or "").lower()))
                continue

            text, err = extract_text(staged, filetype)
            if err:
                record(con, item_key, md5, title, source, path, None, "convert_failed", None,
                       round(size_mb, 2), err)
                n_convert_failed += 1
                continue
            if not text.strip():
                # Real content, but no extractable text — almost always an
                # image-only scanned document (no OCR pass in this script,
                # see docstring point 7). Distinct from a genuine failure.
                record(con, item_key, md5, title, source, path, None, "empty_extraction", 0,
                       round(size_mb, 2), "no text extracted (likely scanned, no OCR)")
                n_convert_failed += 1
                continue

            finalize_ok(item_key, md5, title, source, path, text, size_mb, staged, upload_dir)

    if soffice_queue:
        print(f"  batch-converting {len(soffice_queue)} legacy doc/ppt/xls file(s) via soffice...")
        soffice_results = convert_batch_soffice(
            [(k, staged, ext, ft) for k, staged, ext, _s, _u, _mb, ft in soffice_queue])
        for item_key, staged, _ext, source, upload_dir, size_mb, _ft in soffice_queue:
            md5, title, path, _source, _filetype = item_meta[item_key]
            text, err = soffice_results.get(item_key, (None, "no result"))
            if err:
                record(con, item_key, md5, title, source, path, None, "convert_failed", None,
                       round(size_mb, 2), err)
                n_convert_failed += 1
            elif not text.strip():
                record(con, item_key, md5, title, source, path, None, "empty_extraction", 0,
                       round(size_mb, 2), "no text extracted")
                n_convert_failed += 1
            else:
                finalize_ok(item_key, md5, title, source, path, text, size_mb, staged, upload_dir)

    print(f"  bulk-uploading converted docs back to cloud ({len(sources_uploaded)} source(s))...")
    upload_results = bulk_upload(sources_uploaded)
    for source in sources_uploaded:
        remote = RCLONE_REMOTE_BY_SOURCE[source]
        ok_upload = upload_results.get(source, False)
        # Attribute the batch upload result to every item of that source
        # converted this run (the alternative — per-file confirmation —
        # would reintroduce the N-subprocess-call cost this rewrite removed).
        rows = con.execute(
            "select item_key, title, catalog_path from fulltext_md "
            "where source=? and status='ok' and cloud_status is null",
            [source],
        ).fetchall()
        for item_key, title, catalog_path in rows:
            safe_title = safe_title_for(title)
            if ok_upload:
                con.execute(
                    "update fulltext_md set cloud_status='ok', cloud_pdf_path=?, cloud_md_path=? where item_key=?",
                    [f"{remote}{CLOUD_DEST_FOLDER}/{item_key}_{safe_title}{ext_for(catalog_path)}",
                     f"{remote}{CLOUD_DEST_FOLDER}/{item_key}_{safe_title}.md", item_key],
                )
            else:
                con.execute("update fulltext_md set cloud_status='failed' where item_key=?", [item_key])
                n_cloud_failed += 1

    shutil.rmtree(FETCH_DIR, ignore_errors=True)
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)

    remaining = total_pending - n_ok - n_archive_gone - n_unsupported - n_fetch_failed - n_convert_failed - n_too_large
    print(f"✓ batch complete: {n_ok} converted ({n_ok - n_cloud_failed} cloud-copied, "
          f"{n_cloud_failed} cloud copy failed), {n_archive_gone} archive-not-persisted, "
          f"{n_fetch_failed} fetch failed, {n_convert_failed} convert failed, {n_too_large} too large, "
          f"{n_unsupported} unsupported-source, {mb_read:.0f} MB fetched. "
          f"{remaining} still pending overall.")
    con.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-docs", type=int, default=MAX_DOCS_PER_RUN)
    ap.add_argument("--max-mb", type=float, default=MAX_MB_PER_RUN)
    ap.add_argument("--status", action="store_true", help="Print progress and exit")
    args = ap.parse_args()
    run(args.max_docs, args.max_mb, status_only=args.status)


if __name__ == "__main__":
    main()
