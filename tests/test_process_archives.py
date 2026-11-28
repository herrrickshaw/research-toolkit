"""Tests for process_archives.py. rclone, Zotero and free-disk checks are faked; archives are built in tmp_path."""

import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import process_archives as pa  # noqa: E402
import zotero_dropbox_import as zdi  # noqa: E402

GB = 1024 ** 3


@pytest.fixture
def env(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(pa, "STAGING_DIR", str(stage))
    monkeypatch.setattr(pa, "ARCHIVE_LOG_FILE", str(tmp_path / "alog.jsonl"))
    monkeypatch.setattr(pa, "RCLONE_REMOTE", "dropbox:")
    monkeypatch.setattr(zdi, "LOG_FILE", str(tmp_path / "ilog.jsonl"))
    monkeypatch.setattr(pa, "SAFETY_MARGIN_BYTES", 1000)
    monkeypatch.setattr(pa, "free_bytes", lambda: 10_000_000)
    monkeypatch.setattr(pa.time, "sleep", lambda s: None)
    monkeypatch.setattr(zdi.time, "sleep", lambda s: None)
    return tmp_path


def alog(env):
    p = env / "alog.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def ilog(env):
    p = env / "ilog.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def use_rclone(monkeypatch, handler):
    calls = []

    def run(cmd, capture_output=True, text=True, timeout=None):
        calls.append((list(cmd), timeout))
        r = handler(cmd, timeout)
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(pa.subprocess, "run", run)
    return calls


def lsjson(*entries):
    return Proc(0, json.dumps(list(entries)))


def make_zip(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def make_tar(path, members, mode="w"):
    with tarfile.open(path, mode) as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


# ---- constants ----------------------------------------------------------------------

def test_extension_sets():
    assert pa.ARCHIVE_EXTENSIONS == {".zip", ".tar", ".gz", ".tgz"}
    assert pa.UNSUPPORTED_EXTENSIONS == {".rar", ".7z"}


def test_free_bytes_uses_staging_dir(env, monkeypatch):
    monkeypatch.undo()  # drop the stubs; use the real function against a real dir
    monkeypatch.setattr(pa, "STAGING_DIR", str(env))
    assert pa.free_bytes() > 0


# ---- archive log ----------------------------------------------------------------------

def test_load_archive_done(env):
    assert pa.load_archive_done() == set()
    (env / "alog.jsonl").write_text(
        json.dumps({"path": "a.zip", "status": "ok"}) + "\ngarbage\n"
        + json.dumps({"path": "b.zip", "status": "fail"}) + "\n")
    assert pa.load_archive_done() == {"a.zip"}


def test_log_archive_appends(env):
    pa.log_archive({"k": 1})
    pa.log_archive({"k": 2})
    assert [r["k"] for r in alog(env)] == [1, 2]


# ---- listing ----------------------------------------------------------------------------

def test_rclone_lsjson_branches(env, monkeypatch, capsys):
    use_rclone(monkeypatch, lambda c, t: subprocess.TimeoutExpired(c, t))
    assert pa._rclone_lsjson("t", [], 1) is None
    use_rclone(monkeypatch, lambda c, t: Proc(2, "", "oops"))
    assert pa._rclone_lsjson("t", [], 1) == []
    use_rclone(monkeypatch, lambda c, t: Proc(0, "[bad"))
    assert pa._rclone_lsjson("t", [], 1) == []
    use_rclone(monkeypatch, lambda c, t: Proc(0, ""))
    assert pa._rclone_lsjson("t", [], 1) == []
    assert "list failed" in capsys.readouterr().err


def test_find_archives_root_is_not_recursive(env, monkeypatch):
    calls = use_rclone(monkeypatch, lambda c, t: lsjson({"Path": "loose.zip", "Size": 9}))
    assert pa.find_archives_in("") == [("loose.zip", 9)]
    cmd, timeout = calls[0]
    assert cmd[:3] == ["rclone", "lsjson", "dropbox:"] and "--recursive" not in cmd
    assert "--files-only" in cmd and cmd[cmd.index("--include") + 1] == "*.{zip,tar,gz,tgz,rar,7z}"
    assert timeout == 300


def test_find_archives_folder_is_recursive_and_prefixed(env, monkeypatch):
    calls = use_rclone(monkeypatch, lambda c, t: lsjson({"Path": "sub/x.tgz", "Size": 3}, {"Path": "y.zip"}))
    assert pa.find_archives_in("Energy") == [("Energy/sub/x.tgz", 3), ("Energy/y.zip", 0)]
    assert calls[0][0][2] == "dropbox:Energy" and "--recursive" in calls[0][0]


def test_find_archives_subdivides_on_timeout(env, monkeypatch):
    def handler(cmd, timeout):
        target, args = cmd[2], cmd[3:]
        if "--recursive" in args and target == "dropbox:Big":
            return subprocess.TimeoutExpired(cmd, timeout)
        if "--dirs-only" in args and target == "dropbox:Big":
            return lsjson({"Name": "child"})
        if "--include" in args and "--recursive" not in args and target == "dropbox:Big":
            return lsjson({"Path": "direct.zip", "Size": 1})
        if target == "dropbox:Big/child":
            return lsjson({"Path": "c.tar", "Size": 2})
        raise AssertionError((target, args))

    use_rclone(monkeypatch, handler)
    assert pa.find_archives_in("Big") == [("Big/direct.zip", 1), ("Big/child/c.tar", 2)]
    assert [r["stage"] for r in alog(env)] == ["listing-timeout-subdividing"]


def test_find_archives_root_timeout_subdivides_without_prefix(env, monkeypatch):
    def handler(cmd, timeout):
        target, args = cmd[2], cmd[3:]
        if target == "dropbox:" and "--dirs-only" not in args and "--include" in args and cmd.count("--files-only") == 1 \
                and len(args) == len(pa.ARCHIVE_INCLUDE) + 1 and handler.first:
            handler.first = False
            return subprocess.TimeoutExpired(cmd, timeout)
        if "--dirs-only" in args:
            return lsjson()
        return lsjson({"Path": "r.zip", "Size": 5})

    handler.first = True
    use_rclone(monkeypatch, handler)
    assert pa.find_archives_in("") == [("r.zip", 5)]


def test_find_archives_max_depth(env, monkeypatch, capsys):
    use_rclone(monkeypatch, lambda c, t: subprocess.TimeoutExpired(c, t))
    assert pa.find_archives_in("X", max_depth=0) == []
    assert alog(env)[0]["stage"] == "listing-timeout-max-depth"
    assert "still too large" in capsys.readouterr().err


# ---- extract_archive ------------------------------------------------------------------

def test_extract_zip_reports_nested_archives(env):
    z = env / "a.zip"
    make_zip(z, {"doc.pdf": b"p", "inner/x.ZIP": b"z", "y.rar": b"r", "n.7z": b"7", "readme.txt": b"t"})
    out = env / "out"
    out.mkdir()
    ok, err, nested = pa.extract_archive(str(z), str(out), ".zip")
    assert (ok, err) == (True, None)
    assert sorted(nested) == ["inner/x.ZIP", "n.7z", "y.rar"]
    assert (out / "doc.pdf").read_bytes() == b"p"


@pytest.mark.parametrize("ext,mode", [(".tar", "w"), (".tgz", "w:gz"), (".gz", "w:gz")])
def test_extract_tar_variants(env, ext, mode):
    t = env / f"a{ext}"
    make_tar(t, {"d/doc.pdf": b"pdf", "d/more.tar": b"t"}, mode)
    out = env / "out"
    out.mkdir()
    ok, err, nested = pa.extract_archive(str(t), str(out), ext)
    assert (ok, err, nested) == (True, None, ["d/more.tar"])
    assert (out / "d" / "doc.pdf").read_bytes() == b"pdf"


def test_extract_unsupported_extension(env, monkeypatch):
    monkeypatch.setattr(pa, "BSDTAR", None)
    assert pa.extract_archive(str(env / "x"), str(env), ".rar") == (False, "unsupported extension .rar", [])


def test_extract_corrupt_archive_returns_error(env):
    bad = env / "bad.zip"
    bad.write_bytes(b"not a zip")
    ok, err, nested = pa.extract_archive(str(bad), str(env), ".zip")
    assert ok is False and err and nested == []


def test_extract_zip_size_check_boundary(env, monkeypatch):
    z = env / "a.zip"
    make_zip(z, {"doc.pdf": b"x" * 100})
    out = env / "out"
    out.mkdir()
    monkeypatch.setattr(pa, "free_bytes", lambda: 1100)     # free - margin == 100 == total: allowed
    assert pa.extract_archive(str(z), str(out), ".zip")[0] is True
    monkeypatch.setattr(pa, "free_bytes", lambda: 1099)     # one byte short: refused
    ok, err, _ = pa.extract_archive(str(z), str(out), ".zip")
    assert ok is False and "uncompressed size 100 exceeds free space" in err


def test_extract_tar_size_check_boundary(env, monkeypatch):
    t = env / "a.tar"
    make_tar(t, {"doc.pdf": b"x" * 100})
    out = env / "out"
    out.mkdir()
    monkeypatch.setattr(pa, "free_bytes", lambda: 1100)
    assert pa.extract_archive(str(t), str(out), ".tar")[0] is True
    monkeypatch.setattr(pa, "free_bytes", lambda: 1099)
    assert pa.extract_archive(str(t), str(out), ".tar")[0] is False


def test_extract_refusal_leaves_dest_empty(env, monkeypatch):
    z = env / "a.zip"
    make_zip(z, {"doc.pdf": b"x" * 100})
    out = env / "out"
    out.mkdir()
    monkeypatch.setattr(pa, "free_bytes", lambda: 0)
    pa.extract_archive(str(z), str(out), ".zip")
    assert list(out.iterdir()) == []


# ---- process_one_archive ----------------------------------------------------------------

def counters():
    return {"seen": 0, "ok": 0, "fail": 0, "skip": 0}


def fake_download(content_builder):
    """download(archive_path, local) writing a real archive built by content_builder(local)."""
    def dl(path, local):
        content_builder(local)
        return True, ""
    return dl


def test_unsupported_format_is_logged_not_downloaded(env, monkeypatch, capsys):
    monkeypatch.setattr(pa, "BSDTAR", None)
    monkeypatch.setattr(pa, "download", lambda *a: pytest.fail("no download"))
    pa.process_one_archive("x/a.RAR", 1, set(), counters())
    assert alog(env)[0]["stage"] == "unsupported-format"
    assert "[UNSUPPORTED]" in capsys.readouterr().out


def test_too_large_compressed_boundary(env, monkeypatch):
    monkeypatch.setattr(pa, "download", lambda *a: (False, "stop"))
    monkeypatch.setattr(pa, "free_bytes", lambda: 2000)
    pa.process_one_archive("a.zip", 1000, set(), counters())      # size == free - margin: allowed through
    assert alog(env)[-1]["stage"] == "download"
    pa.process_one_archive("b.zip", 1001, set(), counters())      # one over: refused before download
    assert alog(env)[-1] == {**alog(env)[-1], "path": "b.zip", "stage": "too-large-compressed", "size": 1001}


def test_download_failure_logged(env, monkeypatch):
    monkeypatch.setattr(pa, "download", lambda p, l: (False, "net down"))
    pa.process_one_archive("a.zip", 1, set(), counters())
    r = alog(env)[0]
    assert (r["status"], r["stage"], r["error"]) == ("fail", "download", "net down")


def test_extract_failure_cleans_up(env, monkeypatch):
    monkeypatch.setattr(pa, "download", fake_download(lambda p: Path(p).write_bytes(b"junk")))
    pa.process_one_archive("a.zip", 1, set(), counters())
    assert alog(env)[0]["stage"] == "extract"
    assert os.listdir(env / "stage") == []          # download and extract dir both removed


def test_success_processes_only_allowed_document_types(env, monkeypatch, capsys):
    monkeypatch.setattr(pa, "download", fake_download(lambda p: make_zip(p, {
        "keep.pdf": b"1", "sub/deep.DOCX": b"22", "skip.png": b"x", "nested.zip": b"z"})))
    saved = []

    def save(real_path, title, virtual_path):
        saved.append((title, virtual_path, Path(real_path).read_bytes()))
        return True, 204, b""

    monkeypatch.setattr(zdi, "save_to_zotero", save)
    c = counters()
    pa.process_one_archive("F/pack.zip", 5, set(), c)
    assert sorted(saved) == [("deep.DOCX", "F/pack.zip!sub/deep.DOCX", b"22"), ("keep.pdf", "F/pack.zip!keep.pdf", b"1")]
    assert c == {"seen": 2, "ok": 2, "fail": 0, "skip": 0}
    rec = alog(env)[-1]
    assert rec["status"] == "ok" and rec["files_found"] == 2 and rec["nested_archives"] == ["nested.zip"]
    assert os.listdir(env / "stage") == []
    assert sorted(r["path"] for r in ilog(env)) == ["F/pack.zip!keep.pdf", "F/pack.zip!sub/deep.DOCX"]
    assert "nested archive(s) found inside" in capsys.readouterr().out


def test_success_with_no_documents_still_logged_ok(env, monkeypatch):
    monkeypatch.setattr(pa, "download", fake_download(lambda p: make_zip(p, {"a.txt": b"t"})))
    pa.process_one_archive("a.zip", 1, set(), counters())
    assert alog(env)[-1]["files_found"] == 0 and alog(env)[-1]["status"] == "ok"


def test_tar_archive_processed(env, monkeypatch):
    monkeypatch.setattr(pa, "download", fake_download(lambda p: make_tar(p, {"a.pdf": b"1"}, "w:gz")))
    monkeypatch.setattr(zdi, "save_to_zotero", lambda *a: (True, 204, b""))
    c = counters()
    pa.process_one_archive("a.tgz", 1, set(), c)
    assert c["ok"] == 1


# ---- _save_extracted_file ----------------------------------------------------------------

def test_save_extracted_file_skip_ok_and_fail(env, monkeypatch, capsys):
    f = env / "real.pdf"
    f.write_bytes(b"x")
    c = counters()
    monkeypatch.setattr(zdi, "save_to_zotero", lambda *a: pytest.fail("skipped"))
    pa._save_extracted_file(str(f), "a.zip!real.pdf", 1, {"a.zip!real.pdf"}, c)
    assert c == {"seen": 1, "ok": 0, "fail": 0, "skip": 1}

    monkeypatch.setattr(zdi, "save_to_zotero", lambda *a: (True, 204, b""))
    pa._save_extracted_file(str(f), "a.zip!other.pdf", 7, set(), c)
    assert c["ok"] == 1 and ilog(env)[-1]["size"] == 7

    monkeypatch.setattr(zdi, "save_to_zotero", lambda *a: (False, 500, "e" * 400))
    pa._save_extracted_file(str(f), "a.zip!third.pdf", 1, set(), c)
    assert c["fail"] == 1 and len(ilog(env)[-1]["body"]) == 300
    assert "[ZOTERO-FAIL]" in capsys.readouterr().out


# ---- main -------------------------------------------------------------------------------

def test_main_skips_excluded_folders_and_finished_archives(env, monkeypatch, capsys):
    (env / "alog.jsonl").write_text(json.dumps({"path": "A/done.zip", "status": "ok"}) + "\n")
    monkeypatch.setattr(pa, "get_top_level_folders", lambda: ["A", "Zotero", "B"])
    monkeypatch.setattr(pa, "EXCLUDE_TOP_FOLDERS", {"Zotero"})
    found = {"": [("loose.zip", 1)], "A": [("A/done.zip", 1), ("A/new.zip", 2)], "B": []}
    monkeypatch.setattr(pa, "find_archives_in", lambda f: found[f])
    seen = []
    monkeypatch.setattr(pa, "process_one_archive", lambda p, s, d, c: seen.append((p, s)))
    pa.main()
    assert seen == [("loose.zip", 1), ("A/new.zip", 2)]
    assert "across 3 top-level locations" in capsys.readouterr().out


def test_zip_size_check_uses_uncompressed_not_compressed_size(env, monkeypatch):
    z = env / "big.zip"
    with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.pdf", b"a" * 100_000)           # compresses to a few hundred bytes
    out = env / "out"
    out.mkdir()
    monkeypatch.setattr(pa, "free_bytes", lambda: 50_000 + 1000)   # fits the compressed size, not the real one
    ok, err, _ = pa.extract_archive(str(z), str(out), ".zip")
    assert ok is False and "uncompressed size 100000 exceeds free space" in err
