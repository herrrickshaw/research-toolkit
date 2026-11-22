"""Tests for build_duckdb_catalog.py, reconstruct_dropbox_log.py, organize_topic_collections.py
and check_new_folders.py, against an in-memory fake Zotero API."""

import json
import os
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakezotero import FakeCRUD, FakeZotero, item  # noqa: E402

import build_duckdb_catalog as cat  # noqa: E402
import organize_topic_collections as otc  # noqa: E402
import reconstruct_dropbox_log as rec  # noqa: E402


REAL_CONNECT = duckdb.connect

# ============================== build_duckdb_catalog ==============================

def test_tag_value_returns_first_match_suffix():
    tags = [{"tag": "category:research"}, {"tag": "topic:A"}, {"tag": "topic:B"}]
    assert cat.tag_value(tags, "topic:") == "A"
    assert cat.tag_value(tags, "year:") is None
    assert cat.tag_value([], "x") is None


def test_has_tag_exact_only():
    assert cat.has_tag([{"tag": "status:duplicate"}], "status:duplicate")
    assert not cat.has_tag([{"tag": "status:duplicate2"}], "status:duplicate")


@pytest.mark.parametrize("url,expected", [
    (None, (None, None)), ("", (None, None)), ("plain", (None, None)),
    ("dropbox://a/b", ("dropbox", "a/b")),
])
def test_source_path(url, expected):
    assert cat.source_path(url) == expected


@pytest.fixture
def cenv(tmp_path, monkeypatch):
    monkeypatch.setattr(cat, "DB_PATH", str(tmp_path / "cat.duckdb"))
    monkeypatch.setattr(cat, "CHECKPOINT_FILE", str(tmp_path / "ckpt.json"))
    return tmp_path


def run_cat(monkeypatch, items):
    fz = FakeZotero(items).install(monkeypatch, cat)
    # fts needs a downloadable extension; stub the connection's index step out
    real = REAL_CONNECT

    class Con:
        def __init__(self, c):
            self._c = c
            self.sql = []

        def execute(self, q, *a):
            if "fts" in q.lower():
                self.sql.append(q)
                return None
            return self._c.execute(q, *a)

        def __getattr__(self, n):
            return getattr(self._c, n)

    holder = {}

    def connect(path):
        holder["con"] = Con(real(path))
        return holder["con"]

    monkeypatch.setattr(cat.duckdb, "connect", connect)
    cat.main()
    return fz, holder["con"]


def rows(cenv):
    con = REAL_CONNECT(str(cenv / "cat.duckdb"), read_only=True)
    try:
        return con.execute("select * from documents order by item_key").fetchall()
    finally:
        con.close()


def test_catalog_row_mapping(cenv, monkeypatch, capsys):
    items = [
        item("DUP", 1, title="t2", url="dropbox://Energy/x.pdf", md5="m2", mtime=5, dateAdded="2020",
             tags=["category:research", "topic:Energy", "filetype:pdf", "year:2020", "status:duplicate", "duplicate-of:ORIG"]),
        item("ORIG", 1, title="t1", url="googledrive://Energy/x.pdf", md5="m2", mtime=6, dateAdded="2019",
             tags=["category:backup", "topic:T", "filetype:docx"]),
        item("NOURL", 1, title="skip me"),
    ]
    run_cat(monkeypatch, items)
    r = {x[0]: x for x in rows(cenv)}
    assert set(r) == {"DUP", "ORIG"}
    assert r["DUP"] == ("DUP", "t2", "Energy/x.pdf", "dropbox", "research", "Energy", "pdf", "2020", "m2", True, "ORIG", 5, "2020")
    assert r["ORIG"][3:] == ("googledrive", "backup", "T", "docx", None, "m2", False, None, 6, "2019")
    assert "2 rows written this run, 2 total" in capsys.readouterr().out


def test_catalog_builds_fts_index_only_when_complete(cenv, monkeypatch):
    _, con = run_cat(monkeypatch, [item("A", 1, url="dropbox://a/b.pdf")])
    assert any("create_fts_index" in q for q in con.sql)


def test_catalog_upserts_on_rerun(cenv, monkeypatch):
    run_cat(monkeypatch, [item("A", 1, title="old", url="dropbox://a/b.pdf")])
    run_cat(monkeypatch, [item("A", 1, title="new", url="dropbox://a/b.pdf")])
    assert [(x[0], x[1]) for x in rows(cenv)] == [("A", "new")]


def test_catalog_failure_keeps_checkpoint_and_skips_index(cenv, monkeypatch, capsys):
    many = [item(f"K{i}", 1, url=f"dropbox://f/{i}.pdf") for i in range(150)]
    fz = FakeZotero(many).install(monkeypatch, cat)
    fz.override(lambda m, p: "start=100" in p, [(500, b"")] * 10)
    cat.main()
    out = capsys.readouterr().out
    assert "complete=False" in out and "giving up for now" in out
    assert json.loads((cenv / "ckpt.json").read_text()) == {"start": 100}
    assert len(rows(cenv)) == 100


def test_catalog_resumes_from_checkpoint(cenv, monkeypatch, capsys):
    many = [item(f"K{i}", 1, url=f"dropbox://f/{i}.pdf") for i in range(150)]
    (cenv / "ckpt.json").write_text(json.dumps({"start": 100}))
    run_cat(monkeypatch, many)
    assert len(rows(cenv)) == 50
    assert not (cenv / "ckpt.json").exists()
    assert "Resuming from checkpoint: start=100" in capsys.readouterr().out


def test_fetch_page_retries_then_returns_none(cenv, monkeypatch):
    fz = FakeZotero([]).install(monkeypatch, cat)
    fz.override(lambda m, p: True, [(500, b"")] * 10)
    assert cat.fetch_page(FakeCRUD(), 0) is None
    assert len(fz.calls) == 10


def test_fetch_page_empty_body_is_end(cenv, monkeypatch):
    fz = FakeZotero([]).install(monkeypatch, cat)
    fz.override(lambda m, p: True, [(200, b"")])
    assert cat.fetch_page(FakeCRUD(), 0) == []


# ============================== reconstruct_dropbox_log ==============================

@pytest.fixture
def renv(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "OUT_LOG", str(tmp_path / "out.jsonl"))
    monkeypatch.setattr(rec, "CHECKPOINT_FILE", str(tmp_path / "ckpt.json"))
    return tmp_path


def out_records(renv):
    return [json.loads(x) for x in (renv / "out.jsonl").read_text().splitlines()]


def test_reconstruct_writes_only_dropbox_urls(renv, monkeypatch, capsys):
    items = [
        item("A", 1, url="dropbox://x/a.pdf"),
        item("B", 1, url="googledrive://x/b.pdf"),
        item("C", 1, url=""),
        item("D", 1),
        item("E", 1, url="dropbox://y/e.pdf"),
    ]
    FakeZotero(items).install(monkeypatch, rec)
    rec.main()
    assert out_records(renv) == [
        {"path": "x/a.pdf", "status": "ok", "size": 0, "time": 0, "reconstructed": True},
        {"path": "y/e.pdf", "status": "ok", "size": 0, "time": 0, "reconstructed": True},
    ]
    assert "RECONSTRUCT DONE: 2 dropbox paths" in capsys.readouterr().out


def test_reconstruct_does_not_filter_by_tag(renv, monkeypatch):
    # regression guard for the documented bug: items without the source:dropbox tag must still count
    FakeZotero([item("A", 1, url="dropbox://p.pdf", tags=[])]).install(monkeypatch, rec)
    rec.main()
    assert [r["path"] for r in out_records(renv)] == ["p.pdf"]


def test_reconstruct_checkpoint_removed_when_complete(renv, monkeypatch):
    (renv / "ckpt.json").write_text(json.dumps({"start": 0, "reconstructed": 0}))
    FakeZotero([item("A", 1, url="dropbox://p.pdf")]).install(monkeypatch, rec)
    rec.main()
    assert not (renv / "ckpt.json").exists()


def test_reconstruct_resume_appends_and_carries_count(renv, monkeypatch, capsys):
    many = [item(f"K{i}", 1, url=f"dropbox://f/{i}.pdf") for i in range(150)]
    (renv / "out.jsonl").write_text(json.dumps({"path": "PRIOR"}) + "\n")
    (renv / "ckpt.json").write_text(json.dumps({"start": 100, "reconstructed": 100}))
    FakeZotero(many).install(monkeypatch, rec)
    rec.main()
    paths = [r["path"] for r in out_records(renv)]
    assert paths[0] == "PRIOR" and len(paths) == 51   # appended, not truncated
    out = capsys.readouterr().out
    assert "100 already reconstructed" in out
    assert "RECONSTRUCT DONE: 150 dropbox paths" in out


def test_reconstruct_fresh_run_truncates_old_log(renv, monkeypatch):
    (renv / "out.jsonl").write_text("stale\n")
    FakeZotero([item("A", 1, url="dropbox://p.pdf")]).install(monkeypatch, rec)
    rec.main()
    assert "stale" not in (renv / "out.jsonl").read_text()


def test_reconstruct_failure_keeps_checkpoint(renv, monkeypatch, capsys):
    many = [item(f"K{i}", 1, url=f"dropbox://f/{i}.pdf") for i in range(150)]
    fz = FakeZotero(many).install(monkeypatch, rec)
    fz.override(lambda m, p: "start=100" in p, [(500, b"")] * 10)
    rec.main()
    ck = json.loads((renv / "ckpt.json").read_text())
    assert ck == {"start": 100, "reconstructed": 100}
    out = capsys.readouterr().out
    assert "giving up for now" in out and "complete=False" in out


def test_reconstruct_malformed_page_retried(renv, monkeypatch, capsys):
    fz = FakeZotero([item("A", 1, url="dropbox://p.pdf")]).install(monkeypatch, rec)
    fz.override(lambda m, p: m == "GET", [(200, b"{bad")])
    rec.main()
    assert "200-but-malformed-body" in capsys.readouterr().out
    assert len(out_records(renv)) == 1


# ============================== organize_topic_collections ==============================

@pytest.fixture
def oenv(tmp_path, monkeypatch):
    monkeypatch.setattr(otc, "ORGANIZE_LOG_FILE", str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(otc, "ITEMS_CHECKPOINT_FILE", str(tmp_path / "ckpt.json"))
    return tmp_path


def olog(oenv):
    p = oenv / "log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_fetch_all_collections_parses_and_retries(oenv, monkeypatch):
    fz = FakeZotero(collections=[{"key": "C1", "name": "n"}]).install(monkeypatch, otc)
    fz.override(lambda m, p: True, [(500, b""), (200, b"{bad")])
    got = otc.fetch_all_collections(FakeCRUD())
    assert got == [{"data": {"key": "C1", "name": "n"}}]
    assert len(fz.calls) == 3


def test_fetch_all_collections_gives_up_empty(oenv, monkeypatch, capsys):
    fz = FakeZotero().install(monkeypatch, otc)
    fz.override(lambda m, p: True, [(500, b"")] * 10)
    assert otc.fetch_all_collections(FakeCRUD()) == []
    assert "COLLECTIONS-FAIL" in capsys.readouterr().out


def test_fetch_all_collections_empty_body(oenv, monkeypatch):
    fz = FakeZotero().install(monkeypatch, otc)
    fz.override(lambda m, p: True, [(200, b"")])
    assert otc.fetch_all_collections(FakeCRUD()) == []


def test_get_or_create_uses_cache_without_calls(oenv, monkeypatch):
    fz = FakeZotero().install(monkeypatch, otc)
    cache = {("T", "P"): "CACHED"}
    assert otc.get_or_create_collection(FakeCRUD(), "T", "P", cache) == "CACHED"
    assert fz.calls == []


def test_get_or_create_cache_is_keyed_by_parent(oenv, monkeypatch):
    fz = FakeZotero().install(monkeypatch, otc)
    cache = {}
    a = otc.get_or_create_collection(FakeCRUD(), "T", "P1", cache)
    b = otc.get_or_create_collection(FakeCRUD(), "T", "P2", cache)
    again = otc.get_or_create_collection(FakeCRUD(), "T", "P1", cache)
    assert a != b and again == a
    assert len([c for m, c in fz.calls if m == "POST"]) == 2


def test_get_or_create_payload_parent_optional(oenv, monkeypatch):
    fz = FakeZotero().install(monkeypatch, otc)
    otc.get_or_create_collection(FakeCRUD(), "Top", None, {})
    otc.get_or_create_collection(FakeCRUD(), "Kid", "PARENT", {})
    names = {c["name"]: c for c in fz.collections.values()}
    assert "parentCollection" not in names["Top"]
    assert names["Kid"]["parentCollection"] == "PARENT"


def test_get_or_create_retries_malformed_then_raises(oenv, monkeypatch):
    fz = FakeZotero().install(monkeypatch, otc)
    fz.override(lambda m, p: m == "POST", [(200, b"{bad"), (500, b"")])
    key = otc.get_or_create_collection(FakeCRUD(), "T", None, {})
    assert key.startswith("COL")
    fz.override(lambda m, p: m == "POST", [(500, b"err")] * 10)
    with pytest.raises(RuntimeError, match="failed to create collection 'U'"):
        otc.get_or_create_collection(FakeCRUD(), "U", None, {})


def test_get_or_create_missing_success_key_retried(oenv, monkeypatch):
    fz = FakeZotero().install(monkeypatch, otc)
    fz.override(lambda m, p: m == "POST", [(200, {"failed": {}})])
    assert otc.get_or_create_collection(FakeCRUD(), "T", None, {}).startswith("COL")


def test_add_to_collection_results(oenv, monkeypatch):
    fz = FakeZotero([item("A", 4)]).install(monkeypatch, otc)
    zc = FakeCRUD()
    assert otc.add_to_collection(zc, "A", 4, ["C1"], "C1") == "already-in"
    assert fz.patches == []
    assert otc.add_to_collection(zc, "A", 4, ["C1"], "C2") == "ok"
    assert fz.patches == [("A", {"collections": ["C1", "C2"]})]
    assert otc.add_to_collection(zc, "A", 4, [], "C3") == "conflict"   # stale version -> 412
    fz.override(lambda m, p: m == "PATCH", [(500, b"")])
    assert otc.add_to_collection(zc, "A", 5, [], "C3") == "fail:500"


def test_fetch_all_items_only_topic_tagged_first_topic(oenv, monkeypatch):
    items = [
        item("A", 1, url="u", tags=["topic:One", "topic:Two", "x"], collections=["C"]),
        item("B", 1, tags=["other"]),
        item("C", 1, tags=[]),
    ]
    FakeZotero(items).install(monkeypatch, otc)
    got, done = otc.fetch_all_items(FakeCRUD())
    assert done
    assert got == [{"key": "A", "version": 1, "collections": ["C"], "topic": "One", "url": "u"}]


def test_fetch_all_items_failure_checkpoints_and_resumes(oenv, monkeypatch, capsys):
    many = [item(f"K{i}", 1, tags=[f"topic:T{i % 3}"]) for i in range(150)]
    fz = FakeZotero(many).install(monkeypatch, otc)
    fz.override(lambda m, p: m == "GET" and "start=100" in p, [(500, b"")] * 10)
    got, done = otc.fetch_all_items(FakeCRUD())
    assert (len(got), done) == (100, False)
    ck = json.loads((oenv / "ckpt.json").read_text())
    assert ck["start"] == 100 and len(ck["items"]) == 100
    got2, done2 = otc.fetch_all_items(FakeCRUD())
    assert (len(got2), done2) == (150, True)
    assert not (oenv / "ckpt.json").exists()
    assert "Resuming item listing from checkpoint: start=100, 100 items" in capsys.readouterr().out


def test_organize_main_files_items_into_topic_collections(oenv, monkeypatch, capsys):
    items = [
        item("A", 1, url="dropbox://a", tags=["topic:Energy"], collections=[]),
        item("B", 1, url="dropbox://b", tags=["topic:Energy"], collections=[]),
        item("C", 1, url="dropbox://c", tags=["topic:Law"], collections=["OTHER"]),
        item("D", 1, tags=["no-topic"]),
    ]
    fz = FakeZotero(items).install(monkeypatch, otc)
    otc.main()
    topics = {c["name"]: c for c in fz.collections.values()}
    assert set(topics) == {"Topics", "Energy", "Law"}
    assert topics["Energy"]["parentCollection"] == topics["Topics"]["key"]
    assert fz.items["A"]["collections"] == [topics["Energy"]["key"]]
    assert fz.items["C"]["collections"] == ["OTHER", topics["Law"]["key"]]   # existing membership kept
    assert "collections" not in fz.items["D"]
    assert [r["action"] for r in olog(oenv)] == ["filed"] * 3
    assert "filed=3 already-in=0 conflict=0 failed=0 topics=2" in capsys.readouterr().out


def test_organize_main_idempotent_reuses_existing_collections(oenv, monkeypatch, capsys):
    items = [item("A", 1, url="dropbox://a", tags=["topic:Energy"], collections=[])]
    fz = FakeZotero(items).install(monkeypatch, otc)
    otc.main()
    n_cols = len(fz.collections)
    capsys.readouterr()
    otc.main()
    assert len(fz.collections) == n_cols
    assert "filed=0 already-in=1" in capsys.readouterr().out


def test_organize_main_incomplete_listing_exits_before_filing(oenv, monkeypatch, capsys):
    items = [item(f"K{i}", 1, tags=["topic:T"]) for i in range(150)]
    fz = FakeZotero(items).install(monkeypatch, otc)
    fz.override(lambda m, p: m == "GET" and "start=100" in p, [(500, b"")] * 10)
    with pytest.raises(SystemExit) as e:
        otc.main()
    assert e.value.code == 1
    assert fz.patches == []


def test_organize_main_collection_creation_failure_is_isolated(oenv, monkeypatch, capsys):
    items = [
        item("A", 1, url="ua", tags=["topic:Bad"], collections=[]),
        item("B", 1, url="ub", tags=["topic:Good"], collections=[]),
    ]
    fz = FakeZotero(items).install(monkeypatch, otc)
    orig = fz.request

    def req(method, path, headers=None, data=None, timeout=60, retries=2):
        if method == "POST" and data and b'"Bad"' in data:
            return 500, {}, b"no"
        return orig(method, path, headers, data, timeout, retries)

    monkeypatch.setattr(otc, "_request", req)
    otc.main()
    assert [r["action"] for r in olog(oenv)] == ["filed-failed", "filed"]
    assert "filed=1 already-in=0 conflict=0 failed=1" in capsys.readouterr().out


def test_organize_main_conflict_and_patch_failure_counted(oenv, monkeypatch, capsys):
    items = [item(k, 1, url="u", tags=["topic:T"], collections=[]) for k in "ABC"]
    fz = FakeZotero(items).install(monkeypatch, otc)
    fz.override(lambda m, p: m == "PATCH" and p.endswith("/A"), [(412, b"")])
    fz.override(lambda m, p: m == "PATCH" and p.endswith("/B"), [(500, b"")])
    otc.main()
    out = capsys.readouterr().out
    assert "filed=1 already-in=0 conflict=1 failed=1" in out
    assert "[FAIL] B: fail:500" in out


# ============================== check_new_folders ==============================

@pytest.fixture
def folders(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    (d / "ignore_folders.txt").write_text("# comment\nignored\n\n")
    return d


def run_folders(d, names, monkeypatch, capsys):
    listing = d / "list.txt"
    listing.write_text("\n".join(names) + "\n")
    monkeypatch.setattr(sys, "argv", ["check_new_folders.py", str(listing)])
    # Execute the real source with __file__ = the repo file (so coverage attributes it), but make
    # the script's os.path.abspath(__file__) resolve into the scratch dir so no state file lands in the repo.
    real = str(Path(__file__).resolve().parents[1] / "check_new_folders.py")
    orig_abspath = os.path.abspath

    def fake_abspath(p):
        # only redirect calls made by the script itself, not coverage's own path handling
        if p == real and sys._getframe(1).f_code.co_filename == real:
            return str(d / "check_new_folders.py")
        return orig_abspath(p)

    monkeypatch.setattr(os.path, "abspath", fake_abspath)
    exec(compile(Path(real).read_text(), real, "exec"), {"__file__": real, "__name__": "__main__"})
    return capsys.readouterr().out


def test_first_run_records_baseline_and_alerts_nothing(folders, monkeypatch, capsys):
    out = run_folders(folders, ["a", "b", ""], monkeypatch, capsys)
    assert "baseline recorded: 2 top-level folders" in out
    assert (folders / "seen_top_folders.txt").read_text() == "a\nb\n"


def test_later_run_reports_new_and_appends(folders, monkeypatch, capsys):
    run_folders(folders, ["a"], monkeypatch, capsys)
    out = run_folders(folders, ["a", "z", "m"], monkeypatch, capsys)
    assert "NEW TOP-LEVEL FOLDERS since last run: ['m', 'z']" in out
    assert (folders / "seen_top_folders.txt").read_text() == "a\nm\nz\n"
    assert "none" in run_folders(folders, ["a", "z", "m"], monkeypatch, capsys)


def test_ignored_folders_never_reported_or_recorded(folders, monkeypatch, capsys):
    run_folders(folders, ["a"], monkeypatch, capsys)
    out = run_folders(folders, ["a", "ignored"], monkeypatch, capsys)
    assert "none" in out
    assert "ignored" not in (folders / "seen_top_folders.txt").read_text()


def test_removed_folders_do_not_alert(folders, monkeypatch, capsys):
    run_folders(folders, ["a", "b"], monkeypatch, capsys)
    assert "none" in run_folders(folders, ["a"], monkeypatch, capsys)
