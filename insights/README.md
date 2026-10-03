# research_insights — derived from the qmd research corpus

Built daily (08:15, launchd `com.user.researchinsights`) by `build_insights.py` from the
same `.md` files qmd indexes (`md_corpus/`, ~42k converted documents). No LLM calls.
Uploaded with `rclone copy` to `dropbox:research_insights/`.

## Files
| File | What |
|---|---|
| `report_latest.md` | Corpus overview, topic clusters, gaps, write-vs-read, memory↔research links, conflicting market-size claims, near-duplicates |
| `digest_YYYY-MM-DD.md` | Literature documents new since the previous run (first digest is a 7-day baseline) |
| `research_insights.duckdb` | The tables behind the report (below) |

## Tables (DuckDB)
- `docs(item_key, title, category, topic, year, chars, text_hash, cluster, is_canonical, first_seen, converted_at, kind, shareable)`
  — `kind` is `literature` or `own` (your outputs / machine backups); `is_canonical` marks one row per identical text; `title` is NULL unless `shareable` (look the title up locally via `item_key` in `document_catalog.duckdb`).
- `clusters(cluster, label, n_docs, median_year, pct_recent, with_year, n_own)` — 100 TF-IDF/SVD/KMeans topic clusters over literature; `n_own` = your own docs falling in that topic.
- `claims(item_key, text_hash, kind, subject, currency, value, year, snippet)` — regex-extracted market-size figures and CAGRs (literature only).
- `contradictions` — same market phrase + currency + year, different documents, 2–15× apart. Regex output: read the snippets.
- `near_dups` — 97–99.5% similar pairs of different texts (editions/preprints/revisions).
- `memory_links` — for each Claude memory note, the closest literature documents (TF-IDF cosine).

## Query from anywhere with DuckDB
```sql
-- after: rclone copy dropbox:research_insights/research_insights.duckdb .
select label, n_docs, n_own from clusters order by n_docs desc limit 10;
select subject, year, n_docs, lo, hi, ratio from contradictions order by n_docs desc;
```

## Full-text / semantic search in the cloud (real qmd)
`dropbox:qmd_cloud_backup/` holds `md_corpus.tar.zst` (1.3 GB) and `index.sqlite.zst` (2.8 GB),
refreshed nightly 22:45. `research-toolkit/qmd_cloud_restore.sh` restores both on any machine with
rclone + zstd; then `qmd search`/`qmd query` work as locally. Cloud Claude sessions without shell
access can use the Dropbox connector against `research_md_corpus/` (individual `.md` files).
Only ~5% of documents have vector embeddings, so use BM25 (`qmd search`) first.

## Privacy
- Category `personal` is excluded, plus a content filter (statements, invoices, tickets, CVs, the
  author's name in literature) — about 4.9k documents are dropped from every table.
- Titles only appear in reports for `research`-category docs outside `cv/ Documents/ Attachments/
  all unorganised files/`. The `memory` collection is never uploaded; only note *names* and one-line
  descriptions of non-credential notes appear in `memory_links`.

## Known limits
- Topic labels are top TF-IDF terms, not names. A cluster like "sheet, stock, symbol" is a data export.
- Near-empty extractions (scanned PDFs, ~1%) are excluded; no OCR.
- Claim extraction covers "<subject> market … USD X bn … year" patterns only.
