"""Tests for dedupe_items.py against an in-memory fake Zotero API."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakezotero import FakeCRUD, FakeZotero, item  # noqa: E402

import dedupe_items as dd  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dd, "DEDUPE_LOG_FILE", str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(dd, "LISTING_CHECKPOINT_FILE", str(tmp_path / "ckpt.json"))
    return tmp_path


def make(monkeypatch, items):
    return FakeZotero(items).install(monkeypatch, dd)


def tags_of(fz, key):
    return [t["tag"] for t in fz.items[key]["tags"]]


def log_records(env):
    p = env / "log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---- helpers ------------------------------------------------------------------

def test_already_marked_prefix_match():
    tags = [{"tag": "has-duplicates:3"}, {"tag": "x"}]
    assert dd.already_marked(tags, "has-duplicates:")
    assert not dd.already_marked(tags, "status:duplicate")
    assert not dd.already_marked([], "x")


def test_already_marked_requires_prefix_not_substring():
    assert not dd.already_marked([{"tag": "my-status:duplicate"}], "status:duplicate")


def test_log_action_appends_jsonl_with_timestamp(env):
    dd.log_action({"a": 1})
    dd.log_action({"a": 2})
    recs = log_records(env)
    assert [r["a"] for r in recs] == [1, 2]
    assert all(isinstance(r["time"], float) for r in recs)


def test_patch_tags_sends_version_precondition(env, monkeypatch):
    fz = make(monkeypatch, [item("A", version=5)])
    assert dd.patch_tags(FakeCRUD(), "A", 5, [{"tag": "t"}]) == 204
    assert fz.patches == [("A", {"tags": [{"tag": "t"}]})]
    assert dd.patch_tags(FakeCRUD(), "A", 5, [{"tag": "u"}]) == 412  # version moved on


# ---- fetch_all_items ----------------------------------------------------------------

def test_fetch_keeps_only_items_with_md5_and_paginates(env, monkeypatch):
    items = [item(f"K{i}", md5=("m%d" % i if i % 2 == 0 else ""), dateAdded=f"2020-01-0{i % 9 + 1}", url=f"u{i}")
             for i in range(250)]
    fz = make(monkeypatch, items)
    got, done = dd.fetch_all_items(FakeCRUD())
    assert done is True
    assert [i["key"] for i in got] == [f"K{i}" for i in range(250) if i % 2 == 0]
    assert got[0] == {"key": "K0", "version": 1, "md5": "m0", "dateAdded": "2020-01-01", "tags": [], "url": "u0"}
    starts = [p for m, p in fz.calls if "start=" in p]
    assert any("start=200" in p for p in starts)


def test_fetch_missing_optional_fields_default(env, monkeypatch):
    fz = FakeZotero([{"key": "A", "version": 1, "md5": "m"}]).install(monkeypatch, dd)
    got, _ = dd.fetch_all_items(FakeCRUD())
    assert got[0]["dateAdded"] == "" and got[0]["tags"] == [] and got[0]["url"] == ""


def test_fetch_retries_bad_status_then_succeeds(env, monkeypatch):
    fz = make(monkeypatch, [item("A", md5="m")])
    fz.override(lambda m, p: "start=0" in p, [(500, b""), (200, b"{broken")])
    got, done = dd.fetch_all_items(FakeCRUD())
    assert done and [i["key"] for i in got] == ["A"]
    assert sum("start=0" in p for _, p in fz.calls) == 3


def test_fetch_gives_up_after_ten_failures_and_keeps_checkpoint(env, monkeypatch, capsys):
    fz = make(monkeypatch, [item(f"K{i}", md5="m%d" % i) for i in range(150)])
    fz.override(lambda m, p: "start=100" in p, [(500, b"")] * 10)
    got, done = dd.fetch_all_items(FakeCRUD())
    assert done is False
    assert len(got) == 100
    assert sum("start=100" in p for _, p in fz.calls) == 10
    ck = json.loads((env / "ckpt.json").read_text())
    assert ck["start"] == 100 and len(ck["items"]) == 100
    assert "LIST-FAIL" in capsys.readouterr().out


def test_fetch_resumes_from_checkpoint(env, monkeypatch, capsys):
    fz = make(monkeypatch, [item(f"K{i}", md5="m%d" % i) for i in range(150)])
    prior = [{"key": f"K{i}", "version": 1, "md5": "m%d" % i, "dateAdded": "", "tags": [], "url": ""} for i in range(100)]
    (env / "ckpt.json").write_text(json.dumps({"start": 100, "items": prior}))
    got, done = dd.fetch_all_items(FakeCRUD())
    assert done and len(got) == 150
    assert not any("start=0&" in p or p.endswith("start=0") for _, p in fz.calls)
    assert "Resuming item listing from checkpoint: start=100, 100 items" in capsys.readouterr().out


def test_fetch_empty_body_ends_listing(env, monkeypatch):
    fz = make(monkeypatch, [])
    fz.override(lambda m, p: True, [(200, b"")])
    assert dd.fetch_all_items(FakeCRUD()) == ([], True)


# ---- main ---------------------------------------------------------------------------

def dup_items():
    return [
        item("NEW", 1, md5="same", dateAdded="2021-05-01", url="dropbox://new"),
        item("OLD", 1, md5="same", dateAdded="2019-01-01", url="dropbox://old", tags=["keep"]),
        item("MID", 1, md5="same", dateAdded="2020-01-01", url="dropbox://mid"),
        item("SOLO", 1, md5="alone", dateAdded="2018-01-01", url="dropbox://solo"),
    ]


def test_main_tags_earliest_as_canonical(env, monkeypatch, capsys):
    fz = make(monkeypatch, dup_items())
    dd.main()
    assert tags_of(fz, "OLD") == ["keep", "has-duplicates:2"]
    assert tags_of(fz, "MID") == ["status:duplicate", "duplicate-of:OLD"]
    assert tags_of(fz, "NEW") == ["status:duplicate", "duplicate-of:OLD"]
    assert tags_of(fz, "SOLO") == []
    out = capsys.readouterr().out
    assert "groups=1 updated=3 skipped=0 failed=0" in out
    assert "Found 1 groups of duplicate content covering 3 items" in out


def test_main_logs_every_change_with_urls(env, monkeypatch):
    make(monkeypatch, dup_items())
    dd.main()
    recs = log_records(env)
    assert [r["action"] for r in recs] == ["mark-canonical", "mark-duplicate", "mark-duplicate"]
    assert recs[0]["url"] == "dropbox://old" and recs[0]["duplicate_count"] == 2
    assert recs[1]["key"] == "MID" and recs[1]["duplicate_of_url"] == "dropbox://old"


def test_main_is_idempotent(env, monkeypatch, capsys):
    fz = make(monkeypatch, dup_items())
    dd.main()
    n = len(fz.patches)
    capsys.readouterr()
    dd.main()
    assert len(fz.patches) == n  # nothing re-patched
    assert "updated=0 skipped=3 failed=0" in capsys.readouterr().out


def test_main_incomplete_listing_exits_without_tagging(env, monkeypatch, capsys):
    fz = make(monkeypatch, dup_items())
    fz.override(lambda m, p: True, [(500, b"")] * 10)
    with pytest.raises(SystemExit) as e:
        dd.main()
    assert e.value.code == 1
    assert fz.patches == []
    assert "Stopping here without tagging" in capsys.readouterr().out


def test_main_removes_listing_checkpoint_after_complete_listing(env, monkeypatch):
    make(monkeypatch, dup_items())
    (env / "ckpt.json").write_text(json.dumps({"start": 0, "items": []}))
    dd.main()
    assert not (env / "ckpt.json").exists()


def test_main_conflict_on_duplicate_counts_as_skipped(env, monkeypatch, capsys):
    fz = make(monkeypatch, dup_items())
    fz.override(lambda m, p: m == "PATCH" and p.endswith("/MID"), [(412, b"")])
    dd.main()
    assert tags_of(fz, "MID") == []
    assert "updated=2 skipped=1 failed=0" in capsys.readouterr().out
    assert not any(r.get("key") == "MID" for r in log_records(env))


def test_main_server_error_counts_as_failed_and_is_logged(env, monkeypatch, capsys):
    fz = make(monkeypatch, dup_items())
    fz.override(lambda m, p: m == "PATCH" and p.endswith("/NEW"), [(500, b"")])
    dd.main()
    out = capsys.readouterr().out
    assert "updated=2 skipped=0 failed=1" in out and "[FAIL] dupe NEW: 500" in out
    assert {"action": "mark-duplicate-failed", "key": "NEW", "status": 500}.items() <= \
        [r for r in log_records(env) if r["action"] == "mark-duplicate-failed"][0].items()


def test_main_canonical_failure_logged_and_conflict_ignored(env, monkeypatch, capsys):
    fz = make(monkeypatch, dup_items())
    fz.override(lambda m, p: m == "PATCH" and p.endswith("/OLD"), [(503, b"")])
    dd.main()
    assert "[FAIL] canonical OLD: 503" in capsys.readouterr().out
    assert any(r["action"] == "mark-canonical-failed" and r["status"] == 503 for r in log_records(env))
    # a 412 on the canonical is neither updated nor failed
    fz2 = make(monkeypatch, dup_items())
    fz2.override(lambda m, p: m == "PATCH" and p.endswith("/OLD"), [(412, b"")])
    capsys.readouterr()
    dd.main()
    assert "updated=2 skipped=0 failed=0" in capsys.readouterr().out


def test_main_canonical_with_existing_marker_is_left_alone(env, monkeypatch):
    items = dup_items()
    items[1]["tags"] = [{"tag": "has-duplicates:"}]  # present but treated as already marked
    fz = make(monkeypatch, items)
    dd.main()
    assert tags_of(fz, "OLD") == ["has-duplicates:"]


def test_main_existing_duplicate_tag_preserved_on_dupe(env, monkeypatch):
    items = dup_items()
    items[0]["tags"] = [{"tag": "status:duplicate"}]
    fz = make(monkeypatch, items)
    dd.main()
    assert tags_of(fz, "NEW") == ["status:duplicate"]
    assert tags_of(fz, "MID") == ["status:duplicate", "duplicate-of:OLD"]
