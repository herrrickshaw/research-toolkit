"""Tests for zotero_dropbox_import.py. rclone (subprocess) and the Zotero client are faked."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import zotero_dropbox_import as zdi  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(zdi, "LOG_FILE", str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(zdi, "STAGING_DIR", str(tmp_path / "stage"))
    (tmp_path / "stage").mkdir()
    monkeypatch.setattr(zdi, "RCLONE_REMOTE", "dropbox:")
    monkeypatch.setattr(zdi, "DROPBOX_IMPORT_COLLECTION_KEY", "")
    monkeypatch.setattr(zdi.time, "sleep", lambda s: None)
    monkeypatch.setattr(zdi, "_crud", None)
    return tmp_path


def recs(env):
    p = env / "log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Rclone:
    """Fake subprocess.run for rclone. handler(cmd, timeout) -> Proc | raises."""

    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append((list(cmd), timeout))
        r = self.handler(cmd, timeout)
        if isinstance(r, BaseException):
            raise r
        return r


def use(monkeypatch, handler):
    r = Rclone(handler)
    monkeypatch.setattr(zdi.subprocess, "run", r)
    return r


def lsjson(*entries):
    return Proc(0, json.dumps(list(entries)))


# ---- constants ------------------------------------------------------------------------

def test_allowed_extensions_exact():
    assert zdi.ALLOWED_EXTENSIONS == {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".epub", ".djvu", ".mobi", ".chm", ".ris"}


def test_excel_not_in_scope_of_main_import():
    # discover_excel_files exists precisely because .xlsx was never imported here
    assert ".xlsx" not in zdi.ALLOWED_EXTENSIONS


# ---- get_crud ----------------------------------------------------------------------

def test_get_crud_is_lazy_singleton(env, monkeypatch):
    made = []
    monkeypatch.setattr(zdi, "ZoteroCRUD", lambda: made.append(1) or object())
    a, b = zdi.get_crud(), zdi.get_crud()
    assert a is b and len(made) == 1


# ---- logging / resume ----------------------------------------------------------------

def test_load_done_missing_file(env):
    assert zdi.load_done() == set()


def test_load_done_only_ok_entries_and_tolerates_garbage(env):
    (env / "log.jsonl").write_text(
        json.dumps({"path": "a", "status": "ok"}) + "\n"
        + "not json\n"
        + json.dumps({"path": "b", "status": "fail"}) + "\n"
        + json.dumps({"path": "c", "status": "ok"}) + "\n")
    assert zdi.load_done() == {"a", "c"}


def test_log_appends(env):
    zdi.log({"x": 1})
    zdi.log({"x": 2})
    assert [r["x"] for r in recs(env)] == [1, 2]


# ---- rclone listing --------------------------------------------------------------------

def test_get_top_level_folders_excludes_zotero_and_organized(env, monkeypatch):
    r = use(monkeypatch, lambda c, t: lsjson({"Name": "Zotero"}, {"Name": "Organized"}, {"Name": "pdf"}, {"Name": "Energy"}))
    assert zdi.get_top_level_folders() == ["pdf", "Energy"]
    assert r.calls[0][0] == ["rclone", "lsjson", "dropbox:", "--dirs-only"]
    assert r.calls[0][1] == 120


def test_get_top_level_folders_failure_returns_empty(env, monkeypatch, capsys):
    use(monkeypatch, lambda c, t: Proc(1, "", "boom"))
    assert zdi.get_top_level_folders() == []
    assert "failed to list top-level folders: boom" in capsys.readouterr().err


def test_get_top_level_folders_empty_stdout(env, monkeypatch):
    use(monkeypatch, lambda c, t: Proc(0, ""))
    assert zdi.get_top_level_folders() == []


def test_rclone_lsjson_branches(env, monkeypatch, capsys):
    use(monkeypatch, lambda c, t: subprocess.TimeoutExpired(c, t))
    assert zdi._rclone_lsjson("t", [], 5) is None
    use(monkeypatch, lambda c, t: Proc(3, "", "bad"))
    assert zdi._rclone_lsjson("t", [], 5) == []
    use(monkeypatch, lambda c, t: Proc(0, "{broken"))
    assert zdi._rclone_lsjson("t", [], 5) == []
    use(monkeypatch, lambda c, t: Proc(0, ""))
    assert zdi._rclone_lsjson("t", [], 5) == []
    r = use(monkeypatch, lambda c, t: lsjson({"Path": "a"}))
    assert zdi._rclone_lsjson("dropbox:x", ["--flag"], 9) == [{"Path": "a"}]
    assert r.calls[0] == (["rclone", "lsjson", "dropbox:x", "--flag"], 9)
    assert "list failed" in capsys.readouterr().err


def test_list_folder_filters_extensions_case_insensitively(env, monkeypatch):
    use(monkeypatch, lambda c, t: lsjson(
        {"Path": "a.PDF", "Size": 5}, {"Path": "sub/b.docx"}, {"Path": "c.xlsx"}, {"Path": "d.txt"},
        {"Path": "noext"}, {"Path": "e.tar.pdf", "Size": 1}))
    assert zdi.list_folder("Energy") == [("Energy/a.PDF", 5), ("Energy/sub/b.docx", 0), ("Energy/e.tar.pdf", 1)]


def test_list_folder_recursive_args_and_timeout(env, monkeypatch):
    r = use(monkeypatch, lambda c, t: lsjson())
    zdi.list_folder("F")
    assert r.calls[0] == (["rclone", "lsjson", "dropbox:F", "--recursive", "--files-only"], 300)


def test_list_folder_subdivides_on_timeout(env, monkeypatch, capsys):
    def handler(cmd, timeout):
        target, args = cmd[2], cmd[3:]
        if "--recursive" in args and target in ("dropbox:Big", "dropbox:Big/inner"):
            return subprocess.TimeoutExpired(cmd, timeout)
        if args == ["--files-only"] and target == "dropbox:Big":
            return lsjson({"Path": "top.pdf", "Size": 1}, {"Path": "skip.png"})
        if args == ["--dirs-only"] and target == "dropbox:Big":
            return lsjson({"Name": "inner"}, {"Name": "ok"})
        if args == ["--files-only"] and target == "dropbox:Big/inner":
            return lsjson({"Path": "i.epub", "Size": 2})
        if args == ["--dirs-only"] and target == "dropbox:Big/inner":
            return lsjson()
        if target == "dropbox:Big/ok":
            return lsjson({"Path": "z.pdf", "Size": 3})
        raise AssertionError((target, args))

    use(monkeypatch, handler)
    got = zdi.list_folder("Big")
    assert got == [("Big/top.pdf", 1), ("Big/inner/i.epub", 2), ("Big/ok/z.pdf", 3)]
    stages = [(r["path"], r["stage"], r.get("depth")) for r in recs(env)]
    assert stages == [("__folder__:Big", "listing-timeout-subdividing", 0),
                      ("__folder__:Big/inner", "listing-timeout-subdividing", 1)]
    assert "timed out listing (depth 0)" in capsys.readouterr().out


def test_list_folder_gives_up_at_max_depth(env, monkeypatch, capsys):
    use(monkeypatch, lambda c, t: subprocess.TimeoutExpired(c, t) if "--recursive" in c else lsjson())
    assert zdi.list_folder("X", max_depth=0) == []
    assert recs(env)[0]["stage"] == "listing-timeout-max-depth"
    assert "still too large to list at max depth 0" in capsys.readouterr().err


def test_list_folder_depth_cap_is_inclusive(env, monkeypatch):
    # With max_depth=1 the root (depth 0) subdivides, the child (depth 1) is the one that gives up.
    def handler(cmd, timeout):
        if "--recursive" in cmd:
            return subprocess.TimeoutExpired(cmd, timeout)
        if cmd[3:] == ["--dirs-only"] and cmd[2] == "dropbox:A":
            return lsjson({"Name": "B"})
        return lsjson()

    use(monkeypatch, handler)
    zdi.list_folder("A", max_depth=1)
    assert [r["stage"] for r in recs(env)] == ["listing-timeout-subdividing", "listing-timeout-max-depth"]


def test_list_folder_subdivide_with_no_subdirs_returns_direct_files(env, monkeypatch):
    def handler(cmd, timeout):
        if "--recursive" in cmd:
            return subprocess.TimeoutExpired(cmd, timeout)
        if cmd[3:] == ["--files-only"]:
            return lsjson({"Path": "only.pdf", "Size": 4})
        return lsjson()

    use(monkeypatch, handler)
    assert zdi.list_folder("F") == [("F/only.pdf", 4)]


def test_list_root_files(env, monkeypatch):
    r = use(monkeypatch, lambda c, t: lsjson({"Path": "a.PDF", "Size": 7}, {"Path": "b.txt"}, {"Path": "c.mobi"}))
    assert zdi.list_root_files() == [("a.PDF", 7), ("c.mobi", 0)]
    assert r.calls[0] == (["rclone", "lsjson", "dropbox:", "--files-only"], 300)


def test_list_root_files_failure(env, monkeypatch):
    use(monkeypatch, lambda c, t: Proc(1))
    assert zdi.list_root_files() == []
    use(monkeypatch, lambda c, t: Proc(0, ""))
    assert zdi.list_root_files() == []


# ---- download ------------------------------------------------------------------------

def test_download_success_and_failure(env, monkeypatch):
    r = use(monkeypatch, lambda c, t: Proc(0, "", "  "))
    assert zdi.download("a/b.pdf", "/tmp/x") == (True, "")
    assert r.calls[0] == (["rclone", "copyto", "dropbox:a/b.pdf", "/tmp/x"], 1800)
    use(monkeypatch, lambda c, t: Proc(1, "", "  " + "e" * 500 + "  "))
    ok, err = zdi.download("a", "b")
    assert ok is False and err == "e" * 300


# ---- save_to_zotero -------------------------------------------------------------------

class StubCRUD:
    def __init__(self, create=(200, {"success": {"0": "ITEMKEY"}}), upload=(True, 204, b"")):
        self.create, self.upload, self.created, self.uploads = create, upload, [], []

    def create_items(self, items):
        self.created.append(items)
        return self.create

    def upload_attachment_file(self, *a, **k):
        self.uploads.append((a, k))
        return self.upload


@pytest.fixture
def stub(env, monkeypatch):
    s = StubCRUD()
    monkeypatch.setattr(zdi, "get_crud", lambda: s)
    return s


def write(env, name, data=b"hello"):
    p = env / name
    p.write_bytes(data)
    return str(p)


def test_save_builds_item_and_uploads(env, stub):
    path = write(env, "doc.pdf", b"hello")
    ok, status, body = zdi.save_to_zotero(path, "doc.pdf", "Energy/doc.pdf")
    assert (ok, status) == (True, 204)
    (item,) = stub.created[0]
    assert item["itemType"] == "attachment" and item["linkMode"] == "imported_file"
    assert item["title"] == "doc.pdf" and item["filename"] == "doc.pdf"
    assert item["contentType"] == "application/pdf"
    assert item["md5"] == hashlib.md5(b"hello").hexdigest()
    assert item["url"] == "dropbox://Energy/doc.pdf"
    assert {"tag": "category:research"} in item["tags"] and {"tag": "topic:Energy"} in item["tags"]
    assert "collections" not in item
    args, kw = stub.uploads[0]
    assert args[0] == "ITEMKEY" and args[1] == path and args[2] == "doc.pdf" and args[3] == "application/pdf"
    assert args[4] == item["md5"] and args[6] == 5          # filesize
    assert kw == {"retries": 2, "timeout": 300}


@pytest.mark.parametrize("name,ctype", [
    ("a.djvu", "image/vnd.djvu"), ("a.MOBI", "application/x-mobipocket-ebook"),
    ("a.chm", "application/vnd.ms-htmlhelp"), ("a.ris", "application/x-research-info-systems"),
    ("a.unknownext", "application/octet-stream"),
])
def test_save_content_type_overrides_and_fallback(env, stub, name, ctype):
    zdi.save_to_zotero(write(env, name), name, "F/" + name)
    assert stub.created[0][0]["contentType"] == ctype


def test_save_adds_collection_when_configured(env, stub, monkeypatch):
    monkeypatch.setattr(zdi, "DROPBOX_IMPORT_COLLECTION_KEY", "COLL")
    zdi.save_to_zotero(write(env, "a.pdf"), "a.pdf", "F/a.pdf")
    assert stub.created[0][0]["collections"] == ["COLL"]


def test_save_source_strips_colon_from_remote(env, stub, monkeypatch):
    monkeypatch.setattr(zdi, "RCLONE_REMOTE", "googledrive:")
    zdi.save_to_zotero(write(env, "a.pdf"), "a.pdf", "F/a.pdf")
    assert stub.created[0][0]["url"] == "googledrive://F/a.pdf"
    assert {"tag": "source:googledrive"} in stub.created[0][0]["tags"]


def test_save_create_failure_returns_failed_payload(env, stub):
    stub.create = (500, {"failed": {"0": {"message": "bad"}}})
    ok, status, body = zdi.save_to_zotero(write(env, "a.pdf"), "a.pdf", "F/a.pdf")
    assert (ok, status) == (False, 500)
    assert json.loads(body) == {"0": {"message": "bad"}}
    assert stub.uploads == []


def test_save_create_failure_without_failed_key_dumps_whole_response(env, stub):
    stub.create = (None, {"error": "conn"})
    ok, status, body = zdi.save_to_zotero(write(env, "a.pdf"), "a.pdf", "F/a.pdf")
    assert (ok, status, json.loads(body)) == (False, None, {"error": "conn"})


def test_save_non_200_with_key_is_still_failure(env, stub):
    stub.create = (201, {"success": {"0": "K"}})
    assert zdi.save_to_zotero(write(env, "a.pdf"), "a.pdf", "F/a.pdf")[0] is False


def test_save_upload_failure_truncates_body(env, stub):
    stub.upload = (False, 500, b"u" * 1000)
    ok, status, body = zdi.save_to_zotero(write(env, "a.pdf"), "a.pdf", "F/a.pdf")
    assert (ok, status) == (False, 500) and len(body) == 300


def test_save_forwards_retries_and_timeout(env, stub):
    zdi.save_to_zotero(write(env, "a.pdf"), "a.pdf", "F/a.pdf", retries=7, timeout=11)
    assert stub.uploads[0][1] == {"retries": 7, "timeout": 11}


# ---- process_file -----------------------------------------------------------------------

def counters():
    return {"seen": 0, "ok": 0, "fail": 0, "skip": 0}


def test_process_file_skips_done(env, monkeypatch):
    monkeypatch.setattr(zdi, "download", lambda *a: pytest.fail("no download"))
    c = counters()
    zdi.process_file("a/b.pdf", 1, {"a/b.pdf"}, c)
    assert c == {"seen": 1, "ok": 0, "fail": 0, "skip": 1}
    assert recs(env) == []


def test_process_file_download_failure(env, monkeypatch, capsys):
    monkeypatch.setattr(zdi, "download", lambda p, l: (False, "no such file"))
    monkeypatch.setattr(zdi, "save_to_zotero", lambda *a: pytest.fail("no save"))
    c = counters()
    zdi.process_file("a/b.pdf", 1, set(), c)
    assert c["fail"] == 1 and c["ok"] == 0
    r = recs(env)[0]
    assert (r["status"], r["stage"], r["error"]) == ("fail", "download", "no such file")
    assert "[DL-FAIL] a/b.pdf: no such file" in capsys.readouterr().out


def test_process_file_success_logs_and_removes_local_copy(env, monkeypatch):
    seen = {}

    def dl(p, local):
        Path(local).write_bytes(b"x")
        seen["local"] = local
        return True, ""

    monkeypatch.setattr(zdi, "download", dl)
    monkeypatch.setattr(zdi, "save_to_zotero", lambda lp, title, dp: (True, 204, b""))
    c = counters()
    zdi.process_file("a/b.pdf", 42, set(), c)
    assert c == {"seen": 1, "ok": 1, "fail": 0, "skip": 0}
    assert recs(env)[0]["status"] == "ok" and recs(env)[0]["size"] == 42
    assert not Path(seen["local"]).exists()
    assert Path(seen["local"]).name.endswith("_b.pdf")
    assert Path(seen["local"]).parent == env / "stage"


def test_process_file_zotero_failure_logged_and_local_removed(env, monkeypatch, capsys):
    seen = {}

    def dl(p, local):
        Path(local).write_bytes(b"x")
        seen["local"] = local
        return True, ""

    monkeypatch.setattr(zdi, "download", dl)
    monkeypatch.setattr(zdi, "save_to_zotero", lambda *a: (False, 500, "b" * 500))
    c = counters()
    zdi.process_file("a/b.pdf", 1, set(), c)
    assert c["fail"] == 1
    r = recs(env)[0]
    assert (r["stage"], r["http_status"]) == ("zotero", 500) and len(r["body"]) == 300
    assert not Path(seen["local"]).exists()
    assert "[ZOTERO-FAIL]" in capsys.readouterr().out


def test_process_file_tolerates_missing_local_file_on_cleanup(env, monkeypatch):
    monkeypatch.setattr(zdi, "download", lambda p, l: (True, ""))  # never writes the file
    monkeypatch.setattr(zdi, "save_to_zotero", lambda *a: (True, 204, b""))
    c = counters()
    zdi.process_file("a/b.pdf", 1, set(), c)
    assert c["ok"] == 1


def test_process_file_progress_line_every_25(env, monkeypatch, capsys):
    monkeypatch.setattr(zdi, "download", lambda p, l: (True, ""))
    monkeypatch.setattr(zdi, "save_to_zotero", lambda *a: (True, 204, b""))
    c = counters()
    for i in range(25):
        zdi.process_file(f"d/{i}.pdf", 1, set(), c)
    out = capsys.readouterr().out
    assert out.count("saved so far") == 1 and "25 saved so far (last: 24.pdf)" in out


def test_process_file_throttles(env, monkeypatch):
    slept = []
    monkeypatch.setattr(zdi.time, "sleep", slept.append)
    monkeypatch.setattr(zdi, "download", lambda p, l: (True, ""))
    monkeypatch.setattr(zdi, "save_to_zotero", lambda *a: (True, 204, b""))
    zdi.process_file("d/a.pdf", 1, set(), counters())
    assert slept == [zdi.THROTTLE_SECONDS]


# ---- main -----------------------------------------------------------------------------

def test_main_scans_root_and_folders_and_summarises(env, monkeypatch, capsys):
    (env / "log.jsonl").write_text(json.dumps({"path": "Energy/old.pdf", "status": "ok"}) + "\n")
    monkeypatch.setattr(zdi, "get_top_level_folders", lambda: ["Energy"])
    monkeypatch.setattr(zdi, "list_root_files", lambda: [("loose.pdf", 1)])
    monkeypatch.setattr(zdi, "list_folder", lambda f: [("Energy/old.pdf", 1), ("Energy/new.pdf", 2)])
    processed = []

    def pf(path, size, done, c):
        processed.append(path)
        c["seen"] += 1
        c["skip" if path in done else "ok"] += 1

    monkeypatch.setattr(zdi, "process_file", pf)
    zdi.main()
    assert processed == ["loose.pdf", "Energy/old.pdf", "Energy/new.pdf"]
    out = capsys.readouterr().out
    assert "Already done: 1 files" in out
    assert "(root loose files)" in out
    assert "DONE: seen=3 ok=2 fail=0 skipped(already done)=1" in out
    assert (env / "stage").is_dir()
