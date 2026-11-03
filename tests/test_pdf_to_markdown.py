"""Characterisation tests for pdf_to_markdown.py.

The golden file was generated from the ORIGINAL (pre-refactor) module with
the same synthetic scenario, so passing means the refactor reproduces its
catalog rows, .md output, cloud layout and printed summaries exactly."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("fitz")
pytest.importorskip("docx")
pytest.importorskip("pptx")

import pdf_to_markdown as m  # noqa: E402
from pdf2md_scenario import run_scenario  # noqa: E402

GOLDEN = Path(__file__).parent / "golden" / "pdf_to_markdown_run.json"


def test_end_to_end_matches_original_behaviour(tmp_path, monkeypatch):
    for name in ("REPO_DIR", "DB_PATH", "MD_OUTPUT_DIR", "STAGE_DIR", "FETCH_DIR", "UPLOAD_DIR",
                 "RCLONE_BIN", "RCLONE_REMOTE_BY_SOURCE", "MARKITDOWN_BIN", "SOFFICE_BIN",
                 "MAX_SINGLE_FILE_MB"):
        monkeypatch.setattr(m, name, getattr(m, name))  # restored after the test
    assert run_scenario(m, tmp_path) == json.loads(GOLDEN.read_text())


@pytest.mark.parametrize("title,expected", [
    ("Report.PDF", "Report"),
    ("a/b:c?.docx", "a_b_c_"),
    (None, "untitled"),
    ("x" * 200, "x" * 80),
])
def test_safe_title_for(title, expected):
    assert m.safe_title_for(title) == expected


@pytest.mark.parametrize("path,expected", [
    ("dir/file.pdf", ".pdf"), ("noext", ".bin"), ("a/b.t$x!t", ".txt"), ("a/b." + "z" * 20, "." + "z" * 10),
])
def test_ext_for(path, expected):
    assert m.ext_for(path) == expected


def test_extract_text_unknown_type_uses_markitdown_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "extract_text_markitdown_fallback", lambda p: ("fallback", None))
    assert m.extract_text(tmp_path / "x.mobi", "mobi") == ("fallback", None)


def test_extract_text_reports_malformed_file(tmp_path):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    text, err = m.extract_text(bad, "DOCX")
    assert text is None and err.startswith("docx extraction error")
