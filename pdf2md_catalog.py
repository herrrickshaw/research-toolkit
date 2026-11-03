"""Catalog table (`fulltext_md`) helpers for pdf_to_markdown.py: schema, outcome rows, --status report."""
from __future__ import annotations

import duckdb


def init_table(con: duckdb.DuckDBPyConnection) -> None:
    """Create the resumable `fulltext_md` table (and migrate older copies)."""
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


def record(con, item_key, md5, title, source, catalog_path, md_path, status, chars, source_mb, error,
           cloud_status=None, cloud_pdf_path=None, cloud_md_path=None) -> None:
    """Insert one terminal outcome row; a re-run never overwrites an existing row."""
    con.execute(
        "insert into fulltext_md values (?,?,?,?,?,?,?,?,?,now(),?,?,?,?) on conflict (item_key) do nothing",
        [item_key, md5, title, source, catalog_path, md_path, status, chars, source_mb, error,
         cloud_status, cloud_pdf_path, cloud_md_path],
    )



def print_status(con, total_docs: int) -> None:
    """`--status`: progress summary from the fulltext_md table; no side effects."""
    done = con.execute("select count(*) from fulltext_md").fetchone()[0]
    ok = con.execute("select count(*) from fulltext_md where status='ok'").fetchone()[0]
    by_status = con.execute("select status, count(*) from fulltext_md group by 1 order by 2 desc").fetchall()
    by_cloud = con.execute(
        "select cloud_status, count(*) from fulltext_md where status='ok' group by 1 order by 2 desc"
    ).fetchall()
    avg_chars = con.execute("select avg(chars) from fulltext_md where status='ok'").fetchone()[0]
    near_empty = con.execute(
        "select count(*) from fulltext_md where status='ok' and chars < 200"
    ).fetchone()[0]
    print(f"catalog documents (non-duplicate, all filetypes): {total_docs}")
    print(f"processed so far:             {done} ({ok} converted)")
    print(f"remaining:                    {total_docs - done}")
    for s, c in by_status:
        print(f"  {s}: {c}")
    print("cloud copy-back (of converted items):")
    for s, c in by_cloud:
        print(f"  {s}: {c}")
    if ok:
        print(f"avg chars/doc: {avg_chars:.0f} | near-empty (<200 chars, likely scanned, no OCR run): "
              f"{near_empty} ({100*near_empty/ok:.1f}%)")
