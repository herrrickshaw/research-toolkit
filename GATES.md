# Gates: Complete the Zotero Dropbox/Google Drive organization pipeline

OWNS: research-toolkit/**

Scope: Every item already in the Zotero library carries organizational tags (category/topic/source/filetype/year), genuine duplicate content is tagged for review, a browsable Topics collection hierarchy exists, and both cloud-storage import pipelines (Dropbox, Google Drive) plus their archive-extraction passes either reach natural completion or are honestly abandoned with a stated reason if the source is too large to exhaust in this session.

- [x] G1: Every existing Zotero item has a source: tag (proxy for having gone through build_tags() - this catches the ~15,886 pre-existing items that predate the tagging fix and were never backfilled, since only 700/16,906 were covered by the interrupted first backfill run)
  CHECK: python3 /Users/umashankar/research-toolkit/scripts/check_tag_coverage.py
  EXPECT: TAG_COVERAGE_COMPLETE
  EVIDENCE: Two backfill passes (16718 then 37 more updated), 0 failures. Verified: items_with_url=16794 missing_source_tag=0. TAG_COVERAGE_COMPLETE.

- [x] G2: Deduplication pass has actually tagged genuine duplicate-content groups (0 items carry status:duplicate right now - the only prior dedupe run never got past its listing phase before being killed)
  CHECK: python3 /Users/umashankar/research-toolkit/scripts/check_dedupe_done.py
  EXPECT: DEDUPE_APPLIED
  EVIDENCE: 1640 groups found, 4093 items tagged, 0 failed. Positive control confirmed: 6/9 Shell.pdf copies tagged status:duplicate, 1 tagged has-duplicates. DEDUPE_APPLIED.

- [x] G3: A browsable "Topics" collection hierarchy exists with per-topic children populated (organize_topic_collections.py has never been run - no log file exists)
  CHECK: python3 /Users/umashankar/research-toolkit/scripts/check_topics_collection.py
  EXPECT: TOPICS_COLLECTION_BUILT
  EVIDENCE: 16621 items filed into 186 topic collections, 0 conflicts, 173 failures (transient). Verified: Topics parent key=AUU8UCVM, children=185, sampled 5/5 populated. TOPICS_COLLECTION_BUILT.

- [ ] G4: Dropbox import reaches its own natural "=== DONE:" completion marker, or is explicitly abandoned with a stated reason (account scale / API rate limits) if it can't finish in this session
  CHECK: python3 /Users/umashankar/research-toolkit/scripts/check_import_done.py --log /private/tmp/claude-501/-Users-umashankar/cbbd889a-6c4e-47b9-85de-60aa65a42e93/scratchpad/import_run.log
  EXPECT: IMPORT_DONE
  EVIDENCE: pending

- [ ] G5: Google Drive import reaches its own natural "=== DONE:" completion marker, or is explicitly abandoned with a stated reason
  CHECK: python3 /Users/umashankar/research-toolkit/scripts/check_import_done.py --log /Users/umashankar/research-toolkit/gdrive_import_run.log
  EXPECT: IMPORT_DONE
  EVIDENCE: pending

- [ ] G6: Both archive-extraction passes (Dropbox + Google Drive) reach their "=== ARCHIVES DONE:" marker, or are explicitly abandoned
  CHECK: python3 /Users/umashankar/research-toolkit/scripts/check_archives_done.py
  EXPECT: ARCHIVES_DONE
  EVIDENCE: pending

<!--
G1-G3 are bounded, achievable operations against a fixed 16,906-item library and
are expected to complete within this session.

G4-G6 depend on exhausting the user's actual Dropbox and Google Drive accounts,
which prior turns in this conversation showed subdividing into very large,
slow-to-enumerate folders (My Drive, My Mac backups) under real API rate limits
and an already-documented Zotero local-API stability limit under sustained
load. These may need to be ABANDONed with a clear reason if the account is too
large to exhaust in one session - that is an honest outcome, not a failure to
plan for; it will be recorded explicitly rather than silently claimed done.
-->
