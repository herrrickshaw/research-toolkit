"""Archives inside archives, and .7z through bsdtar. No network, rclone or Zotero."""
import io
import os
import shutil
import subprocess
import zipfile

import pytest

import process_archives as pa


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def run(monkeypatch, tmp_path):
    saved, logged = [], []
    monkeypatch.setattr(pa, "STAGING_DIR", str(tmp_path / "staging"))
    monkeypatch.setattr(pa, "SAFETY_MARGIN_BYTES", 0)
    monkeypatch.setattr(pa, "log_archive", logged.append)
    monkeypatch.setattr(pa, "_save_extracted_file", lambda real, virtual, *a: saved.append(virtual))
    (tmp_path / "staging").mkdir()

    def go(name, payload):
        def download(_remote, local):
            with open(local, "wb") as f:
                f.write(payload)
            return True, None
        monkeypatch.setattr(pa, "download", download)
        pa.process_one_archive(name, len(payload), set(), {})
        assert os.listdir(tmp_path / "staging") == []  # everything cleaned up
        return saved, logged[-1]

    return go


def test_zip_inside_zip_is_imported(run):
    inner = zip_bytes({"deep.pdf": b"d"})
    saved, rec = run("a.zip", zip_bytes({"top.pdf": b"t", "sub/in.zip": inner}))
    assert sorted(saved) == ["a.zip!sub/in.zip!deep.pdf", "a.zip!top.pdf"]
    assert rec["status"] == "ok" and rec["files_found"] == 2 and rec["nested_archives"] == []


def test_nesting_stops_at_the_depth_limit(run, monkeypatch):
    monkeypatch.setattr(pa, "MAX_NESTED_DEPTH", 1)
    level2 = zip_bytes({"too_deep.pdf": b"x"})
    saved, rec = run("a.zip", zip_bytes({"one.zip": zip_bytes({"two.zip": level2, "ok.pdf": b"1"})}))
    assert saved == ["a.zip!one.zip!ok.pdf"] and rec["nested_archives"] == ["two.zip"]


def test_nested_archive_without_room_is_left_for_retry(run, monkeypatch):
    real = pa.extract_archive
    monkeypatch.setattr(pa, "extract_archive", lambda path, dest, ext: (
        (False, "uncompressed size 9 exceeds free space", [])
        if os.path.basename(path) == "in.zip" else real(path, dest, ext)))
    _, rec = run("a.zip", zip_bytes({"in.zip": zip_bytes({"a.pdf": b"1"})}))
    assert rec["status"] == "fail" and rec["stage"] == "nested-too-large"


@pytest.mark.skipif(not shutil.which("bsdtar"), reason="bsdtar not installed")
def test_7z_is_extracted_and_links_are_ignored(run, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "paper.pdf").write_bytes(b"p")
    secret = tmp_path / "outside.pdf"
    secret.write_bytes(b"secret")
    (src / "link.pdf").symlink_to(secret)
    archive = tmp_path / "a.7z"
    subprocess.run([shutil.which("bsdtar"), "--format", "7zip", "-cf", str(archive),
                    "-C", str(src), "paper.pdf", "link.pdf"], check=True)
    saved, rec = run("a.7z", archive.read_bytes())
    assert saved == ["a.7z!paper.pdf"] and rec["status"] == "ok"
