"""Tests for categorize.py and sweep_all_folders.walk(). rclone is faked at the
subprocess.run level; no remote is contacted and no log is written outside tmp_path."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import categorize  # noqa: E402
import sweep_all_folders as sweep  # noqa: E402

MT = "2026-01-02 03:04:05"


# ---- categorize -----------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("loose.pdf", ("(root)", "")),
    ("Energy/sub/x.pdf", ("Energy", "Energy")),
    ("pdf/Solar Reports/x.pdf", ("Solar Reports", "pdf")),     # generic bucket -> one level deeper
    ("pdf/x.pdf", ("pdf", "pdf")),                             # generic bucket, nothing deeper
    ("zip/Pack 3/a.zip!inner/f.pdf", ("Pack 3", "zip")),
])
def test_extract_topic(path, expected):
    assert categorize.extract_topic(path) == expected


@pytest.mark.parametrize("folder,expected", [
    ("Income Tax 2023", "personal"),
    ("Passport scans", "personal"),
    ("laptop_backup", "backup"),
    ("Downloads", "uncategorized"),
    ("", "uncategorized"),
    ("Hydrogen Economy", "research"),
])
def test_classify_folder(folder, expected):
    assert categorize.classify_folder(folder) == expected


def test_build_tags():
    tags = [t["tag"] for t in categorize.build_tags("Energy/a.zip!x/b.PDF", 1700000000000, "dropbox")]
    assert tags == ["category:research", "topic:Energy", "source:dropbox", "filetype:pdf", "year:2023"]
    assert categorize.year_from_mtime_ms(None) is None


# ---- sweep_all_folders.walk -----------------------------------------------------------

class FakeRun:
    def __init__(self, files, timeouts=()):
        self.files, self.timeouts = files, set(timeouts)

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        target, recursive = cmd[2], "-R" in cmd
        if recursive and target in self.timeouts:
            raise subprocess.TimeoutExpired(cmd, timeout)
        prefix = target if target.endswith(":") else target + "/"
        children = [k[len(prefix):] for k in self.files if k.startswith(prefix)]
        if "--dirs-only" in cmd:
            out = sorted({c.split("/")[0] + "/" for c in children if "/" in c})
        else:
            out = [f"{c}\t1\t{MT}" for c in sorted(children) if recursive or "/" not in c]
        return subprocess.CompletedProcess(cmd, 0, "\n".join(out), "")


@pytest.fixture
def quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(sweep, "SWEEP_LOG", tmp_path / "sweep.log")


def _walk(fake, monkeypatch, folder="A"):
    monkeypatch.setattr(sweep.subprocess, "run", fake)
    found, archives, skipped = [], [], []
    sweep.walk("r:", folder, 0, 4, 10, found, archives, skipped)
    return sorted(f[0] for f in found), archives, skipped


def test_walk_fast_path(quiet, monkeypatch):
    found, archives, _ = _walk(FakeRun({"r:A/x.pdf": 1, "r:A/s/y.zip": 1, "r:A/s/z.png": 1}), monkeypatch)
    assert found == ["A/x.pdf"]
    assert [a["path"] for a in archives] == ["A/s/y.zip"]


def test_walk_subdivides_on_timeout(quiet, monkeypatch):
    fake = FakeRun({"r:A/s1/y.pdf": 1, "r:A/s2/z.docx": 1}, timeouts={"r:A"})
    found, _, skipped = _walk(fake, monkeypatch)
    assert found == ["A/s1/y.pdf", "A/s2/z.docx"] and skipped == []


def test_walk_subdivision_keeps_loose_files(quiet, monkeypatch):
    """Files directly inside a folder that had to be subdivided are not in any
    subfolder, so they must be listed separately or they are silently lost."""
    fake = FakeRun({"r:A/loose.pdf": 1, "r:A/s1/y.pdf": 1}, timeouts={"r:A"})
    found, _, _ = _walk(fake, monkeypatch)
    assert found == ["A/loose.pdf", "A/s1/y.pdf"]
