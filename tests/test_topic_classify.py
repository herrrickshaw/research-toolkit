"""Tests for topic_classify.py: the keyword classifier and the duckdb-writing main()."""

import re
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import topic_classify as tc  # noqa: E402


def test_wb_matches_whole_words_only():
    (pat,) = tc.wb("sql")
    assert re.search(pat, "intro to sql")
    assert not re.search(pat, "mysqlserver")
    assert not re.search(pat, "nosqlish")


@pytest.mark.parametrize("variant", ["object oriented", "object-oriented", "object_oriented", "object  -_ oriented"])
def test_wb_treats_space_hyphen_underscore_alike(variant):
    (pat,) = tc.wb("object oriented")
    assert re.search(pat, variant)


def test_wb_escapes_regex_metacharacters():
    (pat,) = tc.wb("a.b")
    assert re.search(pat, "see a.b here")
    assert not re.search(pat, "see axb here")  # '.' is literal, not a wildcard


def test_wb_returns_one_pattern_per_word():
    assert len(tc.wb("a", "b", "c")) == 3


@pytest.mark.parametrize("title,expected", [
    ("Intro to Machine Learning", "IT / Software / Computer Science"),
    ("Stock Market Valuation Handbook", "Finance / Investing / Markets"),
    ("Hydrogen Electrolyser Costs", "Energy (power, fuel, batteries)"),
    ("Electric Vehicle Powertrains", "Automotive & EVs"),
    ("Sustainable Aviation Fuel Study", "Aviation & Aerospace"),
    ("Clinical Cardiology Review", "Medical / Healthcare"),
    ("Tariff Schedule Notes", "India Trade / Economic Policy"),
    ("Irrigation of Farmland", "Agriculture"),
    ("CBSE Question Paper 2019", "Education / Exams"),
    ("Polymer Catalysis Primer", "Chemistry / Materials Science"),
    ("Ministry of Power Circular", "Government / Policy / Regulation"),
    ("Supreme Court Digest", "Law / Legal"),
    ("Fluid Mechanics Notes", "Engineering (general)"),
    ("Cricket Batting Average Study", "Sports / Cricket"),
    ("Net Zero Pathways", "Environment / Climate"),
    ("My Resume 2024", "Personal & administrative documents"),
    ("Tender Notice Brochure", "Business, tender & corporate documents"),
    ("Wikipedia Dump", "Reference material (Wikipedia / how-to)"),
    ("Quarterly Overview", "Reports & presentations (general)"),
])
def test_classify_each_category(title, expected):
    assert tc.classify(title) == expected


def test_classify_is_case_insensitive():
    assert tc.classify("PYTHON COOKBOOK") == "IT / Software / Computer Science"


@pytest.mark.parametrize("title", [None, "", "zzz qqq"])
def test_classify_unclassified(title):
    assert tc.classify(title) == tc.UNCLASSIFIED


def test_first_matching_category_wins():
    # 'portfolio' is Finance, 'python' is IT; IT is listed first so it wins.
    assert tc.classify("python portfolio optimisation") == "IT / Software / Computer Science"
    # A domain report lands in its subject, not the generic 'report' bucket.
    assert tc.classify("India Tariff Report 2026") == "India Trade / Economic Policy"


def test_all_category_patterns_compile_and_names_unique():
    names = [n for n, _ in tc.CATEGORIES]
    assert len(names) == len(set(names))
    for _, pats in tc.CATEGORIES:
        for p in pats:
            re.compile(p)


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    db = tmp_path / "cat.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create table documents(item_key varchar, title varchar)")
    con.executemany("insert into documents values (?,?)", [
        ("a", "Python Cookbook"), ("b", "Stock Market Primer"), ("c", "zzz"), ("d", None),
    ])
    con.close()
    monkeypatch.setattr(tc, "DB_PATH", str(db))
    return db


def _auto(db):
    con = duckdb.connect(str(db), read_only=True)
    try:
        return dict(con.execute("select item_key, auto_topic from documents").fetchall())
    finally:
        con.close()


def test_main_writes_auto_topic(catalog, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["topic_classify.py"])
    tc.main()
    out = capsys.readouterr().out
    assert "4 documents classified" in out
    assert "auto_topic column written for 4 rows" in out
    assert _auto(catalog) == {
        "a": "IT / Software / Computer Science",
        "b": "Finance / Investing / Markets",
        "c": tc.UNCLASSIFIED,
        "d": tc.UNCLASSIFIED,
    }


def test_main_is_idempotent_and_overwrites(catalog, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["topic_classify.py"])
    tc.main()
    con = duckdb.connect(str(catalog))
    con.execute("update documents set auto_topic='stale'")
    con.close()
    tc.main()
    assert _auto(catalog)["a"] == "IT / Software / Computer Science"


def test_main_report_mode_does_not_modify(catalog, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["topic_classify.py", "--report"])
    tc.main()
    out = capsys.readouterr().out
    assert "(--report: DB not modified)" in out
    con = duckdb.connect(str(catalog), read_only=True)
    cols = [r[0] for r in con.execute("describe documents").fetchall()]
    con.close()
    assert "auto_topic" not in cols


def test_main_report_lists_counts_sorted_descending(catalog, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["topic_classify.py", "--report"])
    tc.main()
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "%" in ln]
    counts = [int(ln.split("(")[0]) for ln in lines]
    assert counts == sorted(counts, reverse=True)
    assert counts[0] == 2  # the two unclassified rows
    assert "50.0%" in lines[0]
