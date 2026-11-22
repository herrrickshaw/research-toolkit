"""Additional tests for pdf_to_markdown.py: status report, bulk fetch/upload failure handling, cloud-retry healing,
disk-margin and size gates, the soffice branch of run(), and CLI parsing. rclone is faked in-process."""

import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pdf_to_markdown as p2m  # noqa: E402


def make_pdf(path, text="Hello PDF world"):
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, val in [("REPO_DIR", repo), ("DB_PATH", repo / "c.duckdb"), ("MD_OUTPUT_DIR", repo / "md"),
                      ("STAGE_DIR", repo / "stage"), ("FETCH_DIR", repo / "stage" / "fetch"),
                      ("UPLOAD_DIR", repo / "stage" / "upload")]:
        monkeypatch.setattr(p2m, name, val)
    monkeypatch.setattr(p2m, "DISK_SAFETY_MARGIN_GB", 0)
    cloud = tmp_path / "cloud"
    cloud.mkdir()
    uploaded = tmp_path / "uploaded"
    state = {"calls": [], "upload_rc": 0, "timeout": None}

    def run(cmd, capture_output=True, text=True, timeout=None):
        state["calls"].append((list(cmd), timeout))
        if state["timeout"] and cmd[1] == state["timeout"]:
            raise subprocess.TimeoutExpired(cmd, timeout)
        if cmd[1] == "copy" and "--files-from" in cmd:
            dst = Path(cmd[3])
            for p in Path(cmd[cmd.index("--files-from") + 1]).read_text().splitlines():
                if (cloud / p).is_file():
                    (dst / p).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cloud / p, dst / p)
            return Proc(0)
        if cmd[1] == "copy":
            if state["upload_rc"] == 0:
                uploaded.mkdir(exist_ok=True)
                shutil.copytree(cmd[2], uploaded, dirs_exist_ok=True)
            return Proc(state["upload_rc"])
        raise AssertionError(cmd)

    monkeypatch.setattr(p2m.subprocess, "run", run)
    con = duckdb.connect(str(repo / "c.duckdb"))
    con.execute("create table documents (item_key varchar, md5 varchar, title varchar, path varchar,"
                " source varchar, filetype varchar, is_duplicate boolean)")
    con.close()

    class NS:
        pass

    ns = NS()
    ns.cloud, ns.uploaded, ns.state, ns.repo = cloud, uploaded, state, repo

    def add(key, title, path, source="dropbox", filetype="pdf", dup=False, content=None):
        if content is not None:
            f = cloud / path
            f.parent.mkdir(parents=True, exist_ok=True)
            if callable(content):
                content(f)
            else:
                f.write_bytes(content)
        c = duckdb.connect(str(repo / "c.duckdb"))
        c.execute("insert into documents values (?,?,?,?,?,?,?)", [key, None, title, path, source, filetype, dup])
        c.close()

    def rows(cols="item_key, status, cloud_status, chars, error"):
        c = duckdb.connect(str(repo / "c.duckdb"))
        try:
            return {r[0]: r[1:] for r in c.execute(f"select {cols} from fulltext_md").fetchall()}
        finally:
            c.close()

    ns.add, ns.rows = add, rows
    return ns


# ---- pure helpers ------------------------------------------------------------------

def test_init_table_creates_and_is_idempotent():
    con = duckdb.connect()
    p2m.init_table(con)
    p2m.init_table(con)
    cols = [r[0] for r in con.execute("describe fulltext_md").fetchall()]
    assert cols[:3] == ["item_key", "md5", "title"] and "cloud_md_path" in cols


def test_init_table_adds_missing_cloud_columns_to_old_table():
    con = duckdb.connect()
    con.execute("create table fulltext_md(item_key varchar primary key, md5 varchar, title varchar, source varchar, "
                "catalog_path varchar, md_path varchar, status varchar, chars bigint, source_mb double, "
                "converted_at timestamp, error varchar)")
    p2m.init_table(con)
    cols = {r[0] for r in con.execute("describe fulltext_md").fetchall()}
    assert {"cloud_status", "cloud_pdf_path", "cloud_md_path"} <= cols


def test_record_is_insert_once(env):
    con = duckdb.connect()
    p2m.init_table(con)
    p2m.record(con, "K", "m", "t", "dropbox", "p", None, "ok", 5, 1.0, None)
    p2m.record(con, "K", "m2", "t2", "dropbox", "p", None, "fetch_failed", None, None, "boom")
    assert con.execute("select count(*), min(status), min(md5) from fulltext_md").fetchone() == (1, "ok", "m")


def test_md_output_path_uses_key_and_safe_title(env):
    assert p2m.md_output_path("K1", "Bad/Name.pdf") == p2m.MD_OUTPUT_DIR / "K1_Bad_Name.md"


def test_free_disk_gb_positive(env):
    assert p2m.free_disk_gb() > 0


def test_password_protected_pdf_reported_not_crashed(tmp_path):
    import fitz
    f = tmp_path / "locked.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "secret")
    doc.save(str(f), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    doc.close()
    assert p2m.extract_text(f, "pdf") == (None, "password-protected document")


def test_extract_text_filetype_case_and_none(tmp_path, monkeypatch):
    f = make_pdf(tmp_path / "a.pdf")
    assert "Hello PDF world" in p2m.extract_text(f, "PDF")[0]
    monkeypatch.setattr(p2m, "extract_text_markitdown_fallback", lambda p: ("fallback", None))
    assert p2m.extract_text(f, None) == ("fallback", None)
    assert p2m.extract_text(f, "mobi") == ("fallback", None)


def test_markitdown_timeout_is_an_error(tmp_path, monkeypatch):
    def run(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(p2m.subprocess, "run", run)
    monkeypatch.setattr(p2m, "MARKITDOWN_TIMEOUT_S", 7)
    assert p2m.extract_text_markitdown_fallback(tmp_path / "x") == (None, "markitdown timeout after 7s")


def test_markitdown_failure_without_stderr_has_default_message(tmp_path, monkeypatch):
    monkeypatch.setattr(p2m.subprocess, "run", lambda cmd, **k: Proc(1, "", ""))
    assert p2m.extract_text_markitdown_fallback(tmp_path / "x") == (None, "markitdown failed")


def test_markitdown_error_keeps_only_last_500_chars(tmp_path, monkeypatch):
    monkeypatch.setattr(p2m.subprocess, "run", lambda cmd, **k: Proc(1, "", "a" * 600 + "END"))
    err = p2m.extract_text_markitdown_fallback(tmp_path / "x")[1]
    assert len(err) == 500 and err.endswith("END")


def test_soffice_timeout_still_collects_partial_output(env, monkeypatch, tmp_path):
    def run(cmd, **k):
        out = Path(cmd[cmd.index("--outdir") + 1])
        (out / "k1.txt").write_text("partial")
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(p2m.subprocess, "run", run)
    f1, f2 = tmp_path / "a.doc", tmp_path / "b.doc"
    f1.write_text("x")
    f2.write_text("y")
    res = p2m.convert_batch_soffice([("k1", f1, ".doc", "doc"), ("k2", f2, ".doc", "doc")])
    assert res["k1"] == ("partial", None)
    assert res["k2"][0] is None and "no output" in res["k2"][1]


def test_soffice_unknown_filetype_defaults_to_text_filter(env, monkeypatch, tmp_path):
    seen = {}

    def run(cmd, **k):
        seen["filter"] = cmd[cmd.index("--convert-to") + 1]
        return Proc(0)

    monkeypatch.setattr(p2m.subprocess, "run", run)
    f = tmp_path / "a.rtf"
    f.write_text("x")
    p2m.convert_batch_soffice([("k", f, ".rtf", "rtf")])
    assert seen["filter"] == "txt:Text"


# ---- bulk_fetch / bulk_upload ---------------------------------------------------------------

def test_bulk_fetch_command_and_listfile(env):
    env.state["calls"].clear()
    counts = p2m.bulk_fetch({"dropbox": [("k1", "a/b.pdf"), ("k2", "c.pdf")], "googledrive": []})
    assert counts == {"dropbox": 2}
    cmd, timeout = env.state["calls"][0]
    assert cmd[:3] == [p2m.RCLONE_BIN, "copy", "dropbox:"] and cmd[3] == str(p2m.FETCH_DIR / "dropbox")
    assert cmd[cmd.index("--transfers") + 1] == p2m.RCLONE_TRANSFERS and "--ignore-errors" in cmd
    assert (p2m.STAGE_DIR / "fetch_list_dropbox.txt").read_text() == "a/b.pdf\nc.pdf"
    assert timeout == p2m.RCLONE_TIMEOUT_S


def test_bulk_fetch_skips_unknown_source(env):
    assert p2m.bulk_fetch({"onedrive": [("k", "p")]}) == {}
    assert env.state["calls"] == []


def test_bulk_fetch_timeout_is_swallowed_and_still_counted(env):
    env.state["timeout"] = "copy"
    assert p2m.bulk_fetch({"dropbox": [("k", "p.pdf")]}) == {"dropbox": 1}


def test_bulk_upload_branches(env):
    assert p2m.bulk_upload({"onedrive"}) == {"onedrive": True}                 # no remote
    assert p2m.bulk_upload({"dropbox"}) == {"dropbox": True}                   # no local dir
    (p2m.UPLOAD_DIR / "dropbox").mkdir(parents=True)
    assert p2m.bulk_upload({"dropbox"}) == {"dropbox": True}                   # empty dir
    assert env.state["calls"] == []
    (p2m.UPLOAD_DIR / "dropbox" / "f.md").write_text("x")
    assert p2m.bulk_upload({"dropbox"}) == {"dropbox": True}
    cmd, _ = env.state["calls"][0]
    assert cmd[2] == str(p2m.UPLOAD_DIR / "dropbox") and cmd[3] == "dropbox:research_md_corpus/"
    env.state["upload_rc"] = 1
    assert p2m.bulk_upload({"dropbox"}) == {"dropbox": False}
    env.state["timeout"] = "copy"
    assert p2m.bulk_upload({"dropbox"}) == {"dropbox": False}


# ---- retry_cloud_failures -------------------------------------------------------------------

def seed_ok(con, key, md_path, cloud_status=None, source="dropbox", title="T", status="ok"):
    p2m.record(con, key, "m", title, source, "p/x.pdf", md_path, status, 10, 1.0, None, cloud_status=cloud_status)


def test_retry_cloud_failures_noop_when_nothing_to_heal(env, capsys):
    con = duckdb.connect()
    p2m.init_table(con)
    seed_ok(con, "OK", None, cloud_status="ok")
    seed_ok(con, "BAD", None, cloud_status=None, status="fetch_failed")
    p2m.retry_cloud_failures(con)
    assert capsys.readouterr().out == "" and env.state["calls"] == []


def test_retry_cloud_failures_heals_null_and_failed_rows(env, tmp_path, capsys):
    md = tmp_path / "x.md"
    md.write_text("content")
    con = duckdb.connect()
    p2m.init_table(con)
    seed_ok(con, "NULL1", str(md), cloud_status=None, title="A b")
    seed_ok(con, "FAIL1", str(md), cloud_status="failed", title="C")
    seed_ok(con, "DONE", str(md), cloud_status="ok")
    p2m.retry_cloud_failures(con)
    got = dict(con.execute("select item_key, cloud_status || '|' || coalesce(cloud_md_path,'') from fulltext_md").fetchall())
    assert got["NULL1"] == "ok|dropbox:research_md_corpus/NULL1_A b.md"
    assert got["FAIL1"] == "ok|dropbox:research_md_corpus/FAIL1_C.md"
    assert got["DONE"] == "ok|"                       # untouched
    assert sorted(p.name for p in env.uploaded.iterdir()) == ["FAIL1_C.md", "NULL1_A b.md"]
    assert "2/2 healed" in capsys.readouterr().out
    assert not (p2m.UPLOAD_DIR / "dropbox").exists()


def test_retry_cloud_failures_skips_rows_without_md(env, tmp_path, capsys):
    md = tmp_path / "x.md"
    md.write_text("c")
    con = duckdb.connect()
    p2m.init_table(con)
    seed_ok(con, "NOMD", None)
    seed_ok(con, "GONE", str(tmp_path / "missing.md"))
    seed_ok(con, "A1", str(md))
    p2m.retry_cloud_failures(con)
    st = dict(con.execute("select item_key, cloud_status from fulltext_md").fetchall())
    assert st == {"NOMD": None, "GONE": None, "A1": "ok"}
    assert "1/3 healed" in capsys.readouterr().out


def test_retry_cloud_failures_respects_limit(env, tmp_path, capsys):
    md = tmp_path / "x.md"
    md.write_text("c")
    con = duckdb.connect()
    p2m.init_table(con)
    for k in ("A1", "A2", "A3"):
        seed_ok(con, k, str(md))
    p2m.retry_cloud_failures(con, limit=2)
    assert con.execute("select count(*) from fulltext_md where cloud_status='ok'").fetchone()[0] == 2
    assert "2/2 healed" in capsys.readouterr().out


def test_retry_cloud_failures_leaves_rows_when_upload_fails(env, tmp_path):
    md = tmp_path / "x.md"
    md.write_text("c")
    con = duckdb.connect()
    p2m.init_table(con)
    seed_ok(con, "A1", str(md))
    env.state["upload_rc"] = 1
    p2m.retry_cloud_failures(con)
    assert con.execute("select cloud_status from fulltext_md").fetchone() == (None,)


# ---- run(): status ----------------------------------------------------------------------------

def test_run_status_only_reports_without_converting(env, capsys):
    env.add("A", "t", "p/a.pdf", content=lambda f: make_pdf(f))
    con = duckdb.connect(str(p2m.DB_PATH))
    p2m.init_table(con)
    p2m.record(con, "X1", "m", "t", "dropbox", "p", None, "ok", 100, 1.0, None, cloud_status="ok")
    p2m.record(con, "X2", "m", "t", "dropbox", "p", None, "ok", 300, 1.0, None, cloud_status="failed")
    p2m.record(con, "X3", "m", "t", "dropbox", "p", None, "fetch_failed", None, None, "e")
    con.close()
    p2m.run(max_docs=10, max_mb=10, status_only=True)
    out = capsys.readouterr().out
    assert "catalog documents (non-duplicate, all filetypes): 1" in out
    assert "processed so far:             3 (2 converted)" in out
    assert "  ok: 2" in out and "  fetch_failed: 1" in out
    assert "avg chars/doc: 200 | near-empty (<200 chars, likely scanned, no OCR run): 1 (50.0%)" in out
    assert env.state["calls"] == []                     # nothing fetched
    assert env.rows().keys() == {"X1", "X2", "X3"}


def test_run_status_only_without_converted_docs_omits_average_line(env, capsys):
    p2m.run(max_docs=10, max_mb=10, status_only=True)
    out = capsys.readouterr().out
    assert "processed so far:             0 (0 converted)" in out and "avg chars/doc" not in out


# ---- run(): gates and branches ---------------------------------------------------------------

def test_run_stops_when_disk_margin_hit(env, monkeypatch, capsys):
    env.add("A", "t", "p/a.pdf", content=lambda f: make_pdf(f))
    monkeypatch.setattr(p2m, "DISK_SAFETY_MARGIN_GB", 5)
    monkeypatch.setattr(p2m, "free_disk_gb", lambda: 4.99)
    p2m.run(max_docs=10, max_mb=100)
    assert "disk safety margin hit (<5 GB free); stopping." in capsys.readouterr().out
    assert env.state["calls"] == []


def test_run_proceeds_when_free_equals_margin(env, monkeypatch):
    env.add("A", "t", "p/a.pdf", content=lambda f: make_pdf(f))
    monkeypatch.setattr(p2m, "DISK_SAFETY_MARGIN_GB", 5)
    monkeypatch.setattr(p2m, "free_disk_gb", lambda: 5.0)
    p2m.run(max_docs=10, max_mb=100)
    assert env.rows()["A"][0] == "ok"


def test_run_marks_too_large_files(env, monkeypatch):
    env.add("A", "t", "p/a.pdf", content=lambda f: make_pdf(f))
    size_mb = (env.cloud / "p" / "a.pdf").stat().st_size / (1024 * 1024)
    monkeypatch.setattr(p2m, "MAX_SINGLE_FILE_MB", size_mb * 0.9)
    p2m.run(max_docs=10, max_mb=100)
    status, cloud, chars, err = env.rows()["A"]
    assert status == "too_large" and "exceeds PDF2MD_MAX_SINGLE_FILE_MB" in err


def test_run_file_exactly_at_single_file_limit_is_allowed(env, monkeypatch):
    env.add("A", "t", "p/a.pdf", content=lambda f: make_pdf(f))
    size_mb = (env.cloud / "p" / "a.pdf").stat().st_size / (1024 * 1024)
    monkeypatch.setattr(p2m, "MAX_SINGLE_FILE_MB", size_mb)
    p2m.run(max_docs=10, max_mb=100)
    assert env.rows()["A"][0] == "ok"


def test_run_corrupt_document_is_convert_failed_with_error(env):
    env.add("A", "t", "p/a.pdf", content=b"not a pdf at all")
    p2m.run(max_docs=10, max_mb=100)
    status, cloud, chars, err = env.rows()["A"]
    assert status == "convert_failed" and err.startswith("pdf extraction error")


def test_run_summary_line_counts(env, capsys):
    env.add("A", "ok", "p/a.pdf", content=lambda f: make_pdf(f))
    env.add("B", "bad", "p/b.pdf", content=b"junk")
    env.add("C", "zip", "p/z.zip!i.pdf")
    env.add("D", "gone", "p/gone.pdf")
    p2m.run(max_docs=10, max_mb=100)
    out = capsys.readouterr().out
    assert "4 document(s) pending out of 4 catalogued (processed 0 so far). This batch: 4 docs." in out
    assert ("1 converted (1 cloud-copied, 0 cloud copy failed), 1 archive-not-persisted, 1 fetch failed, "
            "1 convert failed, 0 too large, 0 unsupported-source, 0 deferred") in out
    assert "0 still pending overall." in out


def test_run_reports_deferred_by_max_mb(env, capsys):
    for i in range(3):
        env.add(f"K{i}", "t", f"p/{i}.pdf", content=lambda f: make_pdf(f))
    size_mb = (env.cloud / "p" / "0.pdf").stat().st_size / (1024 * 1024)
    p2m.run(max_docs=10, max_mb=size_mb * 1.5)
    out = capsys.readouterr().out
    assert "2 deferred by --max-mb=" in out
    assert len(env.rows()) == 1


def test_run_max_mb_allows_first_file_even_if_over_cap(env):
    env.add("K0", "t", "p/0.pdf", content=lambda f: make_pdf(f))
    p2m.run(max_docs=10, max_mb=0.0000001)
    assert env.rows()["K0"][0] == "ok"


def test_run_soffice_branch_outcomes(env, monkeypatch):
    for key in ("D1", "D2", "D3"):
        env.add(key, key, f"p/{key}.doc", filetype="DOC", content=b"legacy")
    seen = {}

    def fake_soffice(items):
        seen["items"] = items
        return {"D1": ("legacy text", None), "D2": ("   ", None), "D3": (None, "soffice produced no output for this file")}

    monkeypatch.setattr(p2m, "convert_batch_soffice", fake_soffice)
    p2m.run(max_docs=10, max_mb=100)
    r = env.rows()
    assert r["D1"][0] == "ok" and r["D2"][0] == "empty_extraction" and r["D3"][0] == "convert_failed"
    assert r["D3"][3] == "soffice produced no output for this file"
    assert sorted((k, ext, ft) for k, _s, ext, ft in seen["items"]) == [("D1", ".doc", "doc"), ("D2", ".doc", "doc"), ("D3", ".doc", "doc")]
    assert sorted(p.name for p in env.uploaded.iterdir()) == ["D1_D1.doc", "D1_D1.md"]
    assert (p2m.MD_OUTPUT_DIR / "D1_D1.md").read_text() == "legacy text"


def test_run_not_calling_soffice_for_modern_types(env, monkeypatch):
    env.add("A", "t", "p/a.pdf", content=lambda f: make_pdf(f))
    monkeypatch.setattr(p2m, "convert_batch_soffice", lambda items: pytest.fail("not needed"))
    p2m.run(max_docs=10, max_mb=100)


def test_run_cloud_copy_names_use_real_extension(env):
    env.add("A", "My Paper.pdf", "p/a.pdf", content=lambda f: make_pdf(f))
    p2m.run(max_docs=10, max_mb=100)
    con = duckdb.connect(str(p2m.DB_PATH))
    pdf_path, md_path = con.execute("select cloud_pdf_path, cloud_md_path from fulltext_md").fetchone()
    con.close()
    assert pdf_path == "dropbox:research_md_corpus/A_My Paper.pdf"
    assert md_path == "dropbox:research_md_corpus/A_My Paper.md"


def test_run_cleans_stage_dirs(env):
    env.add("A", "t", "p/a.pdf", content=lambda f: make_pdf(f))
    p2m.run(max_docs=10, max_mb=100)
    assert not p2m.FETCH_DIR.exists() and not p2m.UPLOAD_DIR.exists()


# ---- CLI ---------------------------------------------------------------------------------------

def test_main_parses_arguments(monkeypatch):
    seen = {}
    monkeypatch.setattr(p2m, "run", lambda max_docs, max_mb, status_only=False: seen.update(a=(max_docs, max_mb, status_only)))
    monkeypatch.setattr(sys, "argv", ["pdf_to_markdown.py"])
    p2m.main()
    assert seen["a"] == (p2m.MAX_DOCS_PER_RUN, p2m.MAX_MB_PER_RUN, False)
    monkeypatch.setattr(sys, "argv", ["pdf_to_markdown.py", "--max-docs", "7", "--max-mb", "2.5", "--status"])
    p2m.main()
    assert seen["a"] == (7, 2.5, True)


def test_run_max_mb_cap_is_inclusive(env):
    for i in range(2):
        env.add(f"K{i}", "t", f"p/{i}.pdf", content=lambda f: make_pdf(f))
    size_mb = (env.cloud / "p" / "0.pdf").stat().st_size / (1024 * 1024)
    assert (env.cloud / "p" / "1.pdf").stat().st_size == (env.cloud / "p" / "0.pdf").stat().st_size
    p2m.run(max_docs=10, max_mb=size_mb * 2)            # exactly room for both: neither is deferred
    assert sorted(env.rows()) == ["K0", "K1"]
