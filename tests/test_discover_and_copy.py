"""Tests for discover_excel_files.py, discover_folder_files.py and organize_cloud_copies.py.
rclone is faked at subprocess.run; the catalog is a throwaway duckdb in tmp_path."""

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discover_excel_files as dxl  # noqa: E402
import discover_folder_files as dff  # noqa: E402
import organize_cloud_copies as occ  # noqa: E402

SCHEMA = """create table documents(item_key varchar primary key, title varchar, path varchar, source varchar,
  category varchar, topic varchar, filetype varchar, year varchar, md5 varchar, is_duplicate boolean,
  duplicate_of varchar, mtime bigint, date_added timestamp, auto_topic varchar)"""


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "cat.duckdb"
    con = duckdb.connect(str(p))
    con.execute(SCHEMA)
    con.close()
    return p


def table(db):
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute("select item_key,title,path,source,category,topic,filetype,year,mtime from documents order by path").fetchall()
    finally:
        con.close()


def line(path, size="1", mtime="2023-11-14 22:13:20"):
    return f"{path}\t{size}\t{mtime}"


# =========================== shared pure helpers (both discover scripts) ===========================

@pytest.mark.parametrize("mod", [dxl, dff])
def test_item_key_is_deterministic_and_prefixed(mod):
    a = mod.item_key_for("dropbox", "x/a.pdf")
    assert a == mod.item_key_for("dropbox", "x/a.pdf")
    assert a.startswith("XL_") and len(a) == 3 + 12
    assert a != mod.item_key_for("googledrive", "x/a.pdf")     # source is part of the identity
    assert a != mod.item_key_for("dropbox", "x/b.pdf")


@pytest.mark.parametrize("mod", [dxl, dff])
def test_mtime_ms_from_rclone(mod):
    assert mod.mtime_ms_from_rclone("2023-11-14 22:13:20") == 1700000000000
    assert mod.mtime_ms_from_rclone("2023-11-14 22:13:20.123456789 +0000") == 1700000000000   # tail ignored
    assert mod.mtime_ms_from_rclone("garbage") is None
    assert mod.mtime_ms_from_rclone("") is None


def test_both_scripts_derive_identical_keys():
    assert dxl.item_key_for("dropbox", "a/b.xlsx") == dff.item_key_for("dropbox", "a/b.xlsx")


# =========================== discover_excel_files ===========================

def test_excel_list_builds_include_flags_and_parses_rows(monkeypatch):
    seen = {}

    def run(cmd, capture_output=True, text=True, timeout=None):
        seen.update(cmd=cmd, timeout=timeout)
        return Proc(0, line("a.xlsx") + "\n" + "bad line\n" + line("b/c.xls", "5", "2020-01-01 00:00:00") + "\n" + "x\ty\n")

    monkeypatch.setattr(dxl.subprocess, "run", run)
    rows = dxl.list_excel_files("dropbox", "dropbox:xlsx")
    assert rows == [("a.xlsx", "1", "2023-11-14 22:13:20"), ("b/c.xls", "5", "2020-01-01 00:00:00")]
    cmd = seen["cmd"]
    assert cmd[:3] == [dxl.RCLONE_BIN, "lsf", "dropbox:xlsx"] and cmd[3:5] == ["-R", "--files-only"]
    assert [cmd[i + 1] for i, c in enumerate(cmd) if c == "--include"] == ["*.xlsx", "*.xls"]
    assert seen["timeout"] == 1800


def test_excel_list_failure_returns_empty(monkeypatch, capsys):
    monkeypatch.setattr(dxl.subprocess, "run", lambda *a, **k: Proc(1, "", "denied"))
    assert dxl.list_excel_files("dropbox", "dropbox:xlsx") == []
    assert "rclone lsf failed for dropbox: denied" in capsys.readouterr().out


def run_excel(monkeypatch, db, rows_by_remote, argv=()):
    monkeypatch.setattr(dxl, "DB_PATH", db)
    monkeypatch.setattr(dxl, "list_excel_files", lambda source, remote: rows_by_remote.get(remote, []))
    monkeypatch.setattr(sys, "argv", ["discover_excel_files.py", *argv])
    dxl.main()


def test_excel_main_inserts_with_scoped_prefix_and_classification(monkeypatch, db, capsys):
    run_excel(monkeypatch, db, {
        "dropbox:xlsx": [("Budget/2023.xlsx", "10", "2023-11-14 22:13:20")],
        "dropbox:excel": [("loose.xls", "1", "garbage-time")],
        "googledrive:": [("Energy/sheet.xlsx", "2", "2021-06-01 00:00:00")],
    })
    rows = {r[2]: r for r in table(db)}
    assert set(rows) == {"xlsx/Budget/2023.xlsx", "excel/loose.xls", "Energy/sheet.xlsx"}
    k, title, path, source, cat, topic, ft, year, mtime = rows["xlsx/Budget/2023.xlsx"]
    assert (title, source, ft, year, mtime) == ("2023.xlsx", "dropbox", "xlsx", "2023", 1700000000000)
    assert rows["Energy/sheet.xlsx"][3:7] == ("googledrive", "research", "Energy", "xlsx")
    assert rows["excel/loose.xls"][7] is None and rows["excel/loose.xls"][8] is None   # bad mtime -> no year
    assert rows["xlsx/Budget/2023.xlsx"][0] == dxl.item_key_for("dropbox", "xlsx/Budget/2023.xlsx")
    out = capsys.readouterr().out
    assert "total Excel files found: 3" in out and "inserted 3 new row(s)" in out


def test_excel_main_is_idempotent(monkeypatch, db, capsys):
    rows = {"googledrive:": [("Energy/sheet.xlsx", "2", "2021-06-01 00:00:00")]}
    run_excel(monkeypatch, db, rows)
    capsys.readouterr()
    run_excel(monkeypatch, db, rows)
    assert len(table(db)) == 1
    out = capsys.readouterr().out
    assert "inserted 0 new row(s)" in out and "1 already present" in out


def test_excel_main_dry_run_writes_nothing(monkeypatch, db, capsys):
    run_excel(monkeypatch, db, {"googledrive:": [("Energy/sheet.xlsx", "2", "2021-06-01 00:00:00")]}, ["--dry-run"])
    assert table(db) == []
    out = capsys.readouterr().out
    assert "--dry-run: not writing" in out and "Energy/sheet.xlsx" in out


def test_excel_main_nothing_found_returns_early(monkeypatch, db, capsys):
    run_excel(monkeypatch, db, {})
    assert "total Excel files found: 0" in capsys.readouterr().out
    assert table(db) == []


def test_excel_filetype_defaults_when_no_suffix(monkeypatch, db):
    run_excel(monkeypatch, db, {"googledrive:": [("Energy/noext", "2", "2021-06-01 00:00:00")]})
    assert table(db)[0][6] == "xlsx"


def test_excel_scan_targets_cover_dropbox_scopes_and_drive():
    assert dxl.SCAN_TARGETS == [("dropbox", "dropbox:xlsx"), ("dropbox", "dropbox:excel"), ("googledrive", "googledrive:")]
    assert dxl.EXTENSIONS == ("*.xlsx", "*.xls")


# =========================== discover_folder_files ===========================

def test_folder_list_parses_and_reports_failure(monkeypatch, capsys):
    monkeypatch.setattr(dff.subprocess, "run", lambda *a, **k: Proc(0, line("a.pdf") + "\nbad\n"))
    assert dff.list_folder_files("dropbox:word") == [("a.pdf", "1", "2023-11-14 22:13:20")]
    monkeypatch.setattr(dff.subprocess, "run", lambda *a, **k: Proc(1, "", "nope"))
    assert dff.list_folder_files("dropbox:word") == []
    assert "rclone lsf failed for dropbox:word: nope" in capsys.readouterr().out


def test_folder_list_command_has_no_include_filters(monkeypatch):
    seen = {}
    monkeypatch.setattr(dff.subprocess, "run", lambda cmd, **k: seen.update(cmd=cmd) or Proc(0, ""))
    dff.list_folder_files("dropbox:word")
    assert "--include" not in seen["cmd"] and seen["cmd"][2] == "dropbox:word"


def test_doc_extensions_match_converter_scope():
    assert dff.DOC_EXTENSIONS == {"pdf", "docx", "pptx", "xlsx", "doc", "ppt", "xls", "epub", "mobi", "djvu", "chm", "ris", "html", "htm"}


def run_folder(monkeypatch, db, listings, argv):
    monkeypatch.setattr(dff, "DB_PATH", db)
    monkeypatch.setattr(dff, "list_folder_files", lambda target: listings.get(target, []))
    monkeypatch.setattr(sys, "argv", ["discover_folder_files.py", *argv])
    dff.main()


def test_folder_main_adds_only_uncatalogued_paths(monkeypatch, db, capsys):
    con = duckdb.connect(str(db))
    con.execute("insert into documents(item_key,path) values ('Z1','word/old.docx')")
    con.close()
    run_folder(monkeypatch, db, {"dropbox:word": [("old.docx", "1", "2023-11-14 22:13:20"), ("new.docx", "1", "2023-11-14 22:13:20")]},
               ["dropbox:word"])
    paths = [r[2] for r in table(db)]
    assert paths == ["word/new.docx", "word/old.docx"]
    out = capsys.readouterr().out
    assert "catalog already has 1 path(s)" in out and "1 not already catalogued" in out
    assert "inserted 1 new row(s) into documents (1 -> 2)" in out


def test_folder_main_prefix_source_and_classification(monkeypatch, db):
    run_folder(monkeypatch, db, {
        "dropbox:pptx": [("deck.pptx", "1", "2023-11-14 22:13:20")],
        "googledrive:": [("Energy/x.pdf", "1", "2023-11-14 22:13:20")],
    }, ["dropbox:pptx", "googledrive:"])
    rows = {r[2]: r for r in table(db)}
    assert rows["pptx/deck.pptx"][3] == "dropbox" and rows["pptx/deck.pptx"][5] == "pptx"
    assert rows["Energy/x.pdf"][3] == "googledrive" and rows["Energy/x.pdf"][4] == "research"


def test_folder_main_docs_only_filter(monkeypatch, db, capsys):
    run_folder(monkeypatch, db, {"dropbox:misc": [
        ("a.PDF", "1", "2023-11-14 22:13:20"), ("pic.png", "1", "2023-11-14 22:13:20"),
        ("setup.exe", "1", "2023-11-14 22:13:20"), ("noext", "1", "2023-11-14 22:13:20")]},
        ["--docs-only", "dropbox:misc"])
    assert [r[2] for r in table(db)] == ["misc/a.PDF"]
    assert "1 not already catalogued" in capsys.readouterr().out


def test_folder_main_without_docs_only_keeps_everything_with_unknown_filetype(monkeypatch, db):
    run_folder(monkeypatch, db, {"dropbox:misc": [("noext", "1", "2023-11-14 22:13:20"), ("pic.PNG", "1", "2023-11-14 22:13:20")]},
               ["dropbox:misc"])
    fts = {r[2]: r[6] for r in table(db)}
    assert fts == {"misc/noext": "unknown", "misc/pic.PNG": "png"}


def test_folder_main_dry_run_is_read_only(monkeypatch, db, capsys):
    run_folder(monkeypatch, db, {"dropbox:word": [("a.docx", "1", "2023-11-14 22:13:20")]}, ["--dry-run", "dropbox:word"])
    assert table(db) == []
    assert "--dry-run: not writing" in capsys.readouterr().out


def test_folder_main_nothing_new_returns_without_insert(monkeypatch, db, capsys):
    run_folder(monkeypatch, db, {}, ["dropbox:word"])
    assert "total new files across all targets: 0" in capsys.readouterr().out


def test_folder_main_duplicate_item_key_does_not_double_insert(monkeypatch, db):
    run_folder(monkeypatch, db, {"dropbox:word": [("a.docx", "1", "2023-11-14 22:13:20"), ("a.docx", "1", "2023-11-14 22:13:20")]},
               ["dropbox:word"])
    assert len(table(db)) == 1


# =========================== organize_cloud_copies ===========================

@pytest.mark.parametrize("raw,expected", [
    ("plain", "plain"),
    ('a<b>c:d"e/f\\g|h?i*j', "a_b_c_d_e_f_g_h_i_j"),
    ("ctrl\x01char", "ctrl_char"),
    ("  padded  ", "padded"),
    ("", "misc"),
    ("   ", "misc"),
])
def test_safe_component(raw, expected):
    assert occ.safe_component(raw) == expected


def test_safe_component_truncates_to_150():
    assert len(occ.safe_component("x" * 400)) == 150
    assert occ.safe_component("x" * 150) == "x" * 150


@pytest.fixture
def cenv(tmp_path, monkeypatch, db):
    monkeypatch.setattr(occ, "DB_PATH", str(db))
    monkeypatch.setattr(occ, "COPY_LOG_FILE", str(tmp_path / "copy.jsonl"))
    return tmp_path


def clog(cenv):
    p = cenv / "copy.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def seed(db, rows):
    con = duckdb.connect(str(db))
    for r in rows:
        con.execute("insert into documents(item_key,title,path,source,category,topic,is_duplicate) values (?,?,?,?,?,?,?)", r)
    con.close()


def fake_rclone(monkeypatch, handler=None):
    calls = []

    def run(cmd, capture_output=True, text=True, timeout=None):
        calls.append((cmd, timeout))
        if handler:
            r = handler(cmd)
            if isinstance(r, BaseException):
                raise r
            return r
        return Proc(0)

    monkeypatch.setattr(occ.subprocess, "run", run)
    return calls


def test_already_copied_reads_only_ok_and_tolerates_garbage(cenv):
    assert occ.already_copied("ignored") == set()
    (cenv / "copy.jsonl").write_text(json.dumps({"item_key": "A", "status": "ok"}) + "\nnot json\n"
                                     + json.dumps({"item_key": "B", "status": "fail"}) + "\n"
                                     + json.dumps({"item_key": "C", "status": "skip-archive-virtual-path"}) + "\n")
    assert occ.already_copied("ignored") == {"A"}


def test_copy_main_builds_destination_and_rclone_command(cenv, db, monkeypatch, capsys):
    seed(db, [("K1", "T", "Energy/sub/My: file.pdf", "dropbox", "research", "Energy/Solar", False)])
    calls = fake_rclone(monkeypatch)
    occ.main()
    (cmd, timeout), = calls
    assert cmd == ["rclone", "copyto", "dropbox:Energy/sub/My: file.pdf",
                   "dropbox:Organized/research/Energy_Solar/K1_My_ file.pdf", "--retries", "3", "--low-level-retries", "5"]
    assert timeout == 180
    assert clog(cenv)[0]["status"] == "ok" and clog(cenv)[0]["dest"].endswith("K1_My_ file.pdf")
    assert "copied=1 skipped=0 archive_skipped=0 failed=0 (of 1 eligible)" in capsys.readouterr().out


def test_copy_main_filters_ineligible_rows(cenv, db, monkeypatch):
    seed(db, [
        ("DUP", "t", "a/dup.pdf", "dropbox", "research", "a", True),
        ("NOCAT", "t", "a/nocat.pdf", "dropbox", None, "a", False),
        ("OTHER", "t", "a/other.pdf", "onedrive", "research", "a", False),
        ("GOOD", "t", "a/good.pdf", "googledrive", "backup", "a", False),
    ])
    calls = fake_rclone(monkeypatch)
    occ.main()
    assert [c[0][2] for c in calls] == ["googledrive:a/good.pdf"]


def test_copy_main_skips_archive_virtual_paths_and_logs_them(cenv, db, monkeypatch, capsys):
    seed(db, [("ARC", "t", "zip/p.zip!inner/f.pdf", "dropbox", "research", "zip", False)])
    calls = fake_rclone(monkeypatch)
    occ.main()
    assert calls == []
    assert clog(cenv) == [{"item_key": "ARC", "path": "zip/p.zip!inner/f.pdf", "status": "skip-archive-virtual-path"}]
    assert "archive_skipped=1" in capsys.readouterr().out


def test_copy_main_resume_skips_already_copied(cenv, db, monkeypatch, capsys):
    seed(db, [("A", "t", "a/1.pdf", "dropbox", "research", "a", False), ("B", "t", "a/2.pdf", "dropbox", "research", "a", False)])
    (cenv / "copy.jsonl").write_text(json.dumps({"item_key": "A", "status": "ok"}) + "\n")
    calls = fake_rclone(monkeypatch)
    occ.main()
    assert [c[0][2] for c in calls] == ["dropbox:a/2.pdf"]
    out = capsys.readouterr().out
    assert "1 already copied in a prior run" in out and "copied=1 skipped=1" in out


def test_copy_main_failure_and_timeout_logged(cenv, db, monkeypatch, capsys):
    seed(db, [("A", "t", "a/1.pdf", "dropbox", "research", "a", False), ("B", "t", "a/2.pdf", "dropbox", "research", "a", False)])

    def handler(cmd):
        if cmd[2].endswith("1.pdf"):
            return Proc(1, "", "e" * 500)
        return subprocess.TimeoutExpired(cmd, 180)

    fake_rclone(monkeypatch, handler)
    occ.main()
    recs = {r["item_key"]: r for r in clog(cenv)}
    assert recs["A"]["status"] == "fail" and len(recs["A"]["error"]) == 300
    assert recs["B"]["error"] == "rclone hung past 180s timeout"
    assert "copied=0 skipped=0 archive_skipped=0 failed=2" in capsys.readouterr().out


def test_copy_main_empty_topic_falls_back_to_misc(cenv, db, monkeypatch):
    seed(db, [("A", "Title Only", "a/b.pdf", "dropbox", "research", None, False)])
    calls = fake_rclone(monkeypatch)
    occ.main()
    assert calls[0][0][3] == "dropbox:Organized/research/misc/A_b.pdf"


def test_copy_main_progress_line_every_100(cenv, db, monkeypatch, capsys):
    seed(db, [(f"K{i:03d}", "t", f"a/{i}.pdf", "dropbox", "research", "a", False) for i in range(100)])
    fake_rclone(monkeypatch)
    occ.main()
    out = capsys.readouterr().out
    assert out.count("... 100 copied so far") == 1


def test_folder_main_opens_db_read_only_only_for_dry_run(monkeypatch, db):
    seen = []
    real = duckdb.connect
    monkeypatch.setattr(dff.duckdb, "connect", lambda p, read_only=False: seen.append(read_only) or real(p, read_only=read_only))
    run_folder(monkeypatch, db, {}, ["--dry-run", "dropbox:word"])
    run_folder(monkeypatch, db, {}, ["dropbox:word"])
    assert seen == [True, False]
