"""Tests for backfill_tags.py against an in-memory fake Zotero API."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakezotero import FakeCRUD, FakeZotero, item  # noqa: E402

import backfill_tags as bt  # noqa: E402

MT = 1700000000000  # 2023


def T(*names):
    return [{"tag": n} for n in names]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "LISTING_CHECKPOINT_FILE", str(tmp_path / "ckpt.json"))
    return tmp_path


def good_tags(path="Energy/a.pdf", source="dropbox"):
    return [t["tag"] for t in bt.build_tags(path, MT, source)]


# ---- pure helpers -------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    (None, (None, None)),
    ("", (None, None)),
    ("no-scheme/path", (None, None)),
    ("dropbox://a/b.pdf", ("dropbox", "a/b.pdf")),
    ("googledrive://x://y", ("googledrive", "x://y")),  # only the first :// splits
])
def test_source_from_url(url, expected):
    assert bt.source_from_url(url) == expected


def test_derived_category_tags_none_without_source():
    assert bt.derived_category_tags(None, MT) is None
    assert bt.derived_category_tags("plain", MT) is None


def test_derived_category_tags_matches_build_tags():
    got = bt.derived_category_tags("dropbox://Energy/a.pdf", MT)
    assert got == bt.build_tags("Energy/a.pdf", MT, "dropbox")


def test_tags_need_update_equal_sets_in_any_order():
    derived = T("category:research", "topic:E", "source:dropbox", "filetype:pdf", "year:2023")
    existing = list(reversed(derived))
    assert bt.tags_need_update(existing, derived) is False


def test_tags_need_update_ignores_foreign_tags():
    derived = T("category:research", "topic:E", "source:dropbox", "filetype:pdf")
    assert bt.tags_need_update(derived + T("my-own-tag", "status:duplicate"), derived) is False


@pytest.mark.parametrize("existing", [
    [],                                                                   # nothing yet
    ["category:research", "topic:E", "source:dropbox", "filetype:pdf", "year:1999"],   # stale extra year
    ["category:backup", "topic:E", "source:dropbox", "filetype:pdf"],     # wrong value
    ["category:research", "topic:E", "source:dropbox"],                   # missing one
])
def test_tags_need_update_true_when_ours_differ(existing):
    derived = T("category:research", "topic:E", "source:dropbox", "filetype:pdf")
    assert bt.tags_need_update(T(*existing), derived) is True


def test_merge_tags_replaces_ours_keeps_others_in_order():
    existing = T("keep1", "topic:old", "year:1", "keep2", "category:x")
    derived = T("category:research", "topic:new")
    assert bt.merge_tags(existing, derived) == T("keep1", "keep2", "category:research", "topic:new")


def test_patch_item_tags_sends_headers_and_version(env, monkeypatch):
    fz = FakeZotero([item("A", version=3)]).install(monkeypatch, bt)
    assert bt.patch_item_tags("A", 3, T("t"), "S", "K") == 204
    assert bt.patch_item_tags("A", 3, T("t"), "S", "K") == 412
    assert fz.patches == [("A", {"tags": T("t")})]


# ---- main -----------------------------------------------------------------------------

def lib(*extra):
    return [
        item("STALE", 1, url="dropbox://Energy/a.pdf", mtime=MT, tags=["status:duplicate", "category:wrong"]),
        item("FRESH", 1, url="dropbox://Energy/a.pdf", mtime=MT, tags=good_tags()),
        item("NOURL", 1, tags=[]),
        item("BADURL", 1, url="just-a-path", tags=[]),
        *extra,
    ]


def run(env, monkeypatch, items):
    fz = FakeZotero(items).install(monkeypatch, bt)
    bt.main()
    return fz


def test_main_updates_only_stale_items(env, monkeypatch, capsys):
    fz = run(env, monkeypatch, lib())
    assert [k for k, _ in fz.patches] == ["STALE"]
    tags = [t["tag"] for t in fz.items["STALE"]["tags"]]
    assert tags[0] == "status:duplicate"            # foreign tag preserved, first
    assert tags[1:] == good_tags()
    assert "seen=4 updated=1 skipped=3 failed=0 complete=True" in capsys.readouterr().out


def test_main_is_idempotent(env, monkeypatch, capsys):
    fz = run(env, monkeypatch, lib())
    n = len(fz.patches)
    capsys.readouterr()
    bt.main()
    assert len(fz.patches) == n
    assert "updated=0 skipped=4 failed=0" in capsys.readouterr().out


def test_main_conflict_skipped_and_other_failures_counted(env, monkeypatch, capsys):
    items = lib(item("S2", 1, url="dropbox://B/b.pdf", mtime=MT), item("S3", 1, url="dropbox://C/c.pdf", mtime=MT))
    fz = FakeZotero(items).install(monkeypatch, bt)
    fz.override(lambda m, p: m == "PATCH" and p.endswith("/STALE"), [(412, b"")])
    fz.override(lambda m, p: m == "PATCH" and p.endswith("/S2"), [(500, b"")])
    bt.main()
    out = capsys.readouterr().out
    assert "updated=1 skipped=4 failed=1" in out
    assert "[PATCH-FAIL] S2: 500" in out


def test_main_checkpoint_removed_on_completion(env, monkeypatch):
    (env / "ckpt.json").write_text(json.dumps({"start": 0}))
    run(env, monkeypatch, lib())
    assert not (env / "ckpt.json").exists()


def test_main_resumes_from_checkpoint(env, monkeypatch, capsys):
    many = [item(f"K{i}", 1, url=f"dropbox://T/{i}.pdf", mtime=MT) for i in range(150)]
    (env / "ckpt.json").write_text(json.dumps({"start": 100}))
    fz = run(env, monkeypatch, many)
    assert len(fz.patches) == 50                      # only the items past the checkpoint
    assert all(int(k[1:]) >= 100 for k, _ in fz.patches)
    assert "Resuming from checkpoint: start=100" in capsys.readouterr().out


def test_main_listing_failure_keeps_checkpoint_and_reports_incomplete(env, monkeypatch, capsys):
    many = [item(f"K{i}", 1, url=f"dropbox://T/{i}.pdf", mtime=MT) for i in range(150)]
    fz = FakeZotero(many).install(monkeypatch, bt)
    fz.override(lambda m, p: m == "GET" and "start=100" in p, [(500, b"x")] * 10)
    bt.main()
    out = capsys.readouterr().out
    assert "complete=False" in out and "LIST-FAIL" in out
    assert json.loads((env / "ckpt.json").read_text()) == {"start": 100}
    assert len(fz.patches) == 100


def test_main_malformed_page_is_retried(env, monkeypatch, capsys):
    fz = FakeZotero(lib()).install(monkeypatch, bt)
    fz.override(lambda m, p: m == "GET", [(200, b"{oops")])
    bt.main()
    out = capsys.readouterr().out
    assert "200-but-malformed-body" in out and "seen=4" in out


def test_main_empty_library(env, monkeypatch, capsys):
    run(env, monkeypatch, [])
    assert "seen=0 updated=0 skipped=0 failed=0 complete=True" in capsys.readouterr().out
