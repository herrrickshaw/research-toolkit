"""Characterisation tests for categorize.py and topic_classify.py.
`golden/classifiers.json` was produced by the original modules on the same inputs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import categorize
import topic_classify
from classify_inputs import FOLDERS, MTIMES, PATHS, TITLES

GOLDEN = json.loads((Path(__file__).parent / "golden" / "classifiers.json").read_text())


def test_classify_folder_matches_original():
    assert [categorize.classify_folder(f) for f in FOLDERS] == GOLDEN["folder"]


def test_extract_topic_matches_original():
    assert [list(categorize.extract_topic(p)) for p in PATHS] == GOLDEN["topic"]


def test_year_from_mtime_matches_original():
    assert [categorize.year_from_mtime_ms(m) for m in MTIMES] == GOLDEN["year"]


def test_build_tags_matches_original():
    got = [categorize.build_tags(p, m, "dropbox") for p, m in zip(PATHS, (MTIMES * 3)[:len(PATHS)])]
    assert got == GOLDEN["tags"]


def test_topic_classify_matches_original():
    assert [topic_classify.classify(t) for t in TITLES] == GOLDEN["classify"]


def test_root_file_gets_own_bucket():
    assert categorize.extract_topic("loose.pdf") == ("(root)", "")


def test_wb_treats_separators_as_interchangeable():
    pat = topic_classify.wb("object oriented")[0]
    import re
    assert all(re.search(pat, s) for s in ("object oriented", "object-oriented", "object_oriented"))


def _make_db(path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(path))
    con.execute("create table documents (item_key varchar, title varchar)")
    con.executemany("insert into documents values (?,?)",
                    [("a", "Machine Learning Basics"), ("b", "Zzz"), ("c", "Bank statement")])
    con.close()


def test_main_report_does_not_modify_db(tmp_path, capsys):
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "c.duckdb"
    _make_db(db)
    topic_classify.main(["--report"], db_path=str(db))
    out = capsys.readouterr().out
    assert "3 documents classified" in out and "(--report: DB not modified)" in out
    cols = [r[0] for r in duckdb.connect(str(db)).execute("describe documents").fetchall()]
    assert "auto_topic" not in cols


def test_main_writes_auto_topic_and_is_idempotent(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "c.duckdb"
    _make_db(db)
    for _ in range(2):
        topic_classify.main([], db_path=str(db))
    rows = dict(duckdb.connect(str(db)).execute("select item_key, auto_topic from documents").fetchall())
    assert rows == {"a": "IT / Software / Computer Science", "b": topic_classify.UNCLASSIFIED,
                    "c": "Personal & administrative documents"}
