"""Characterisation test for sweep_all_folders.main(): fake rclone, temp catalog/logs, no network.

Golden captured from the code before the pass-3 split."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

import categorize  # noqa: E402,F401  (cache the repo copy before the sweep module's sys.path insert)
import sweep_all_folders as sw  # noqa: E402

GOLDEN = Path(__file__).parent / "golden" / "sweep_all_folders_run.json"

# (remote, listed target, recursive?) -> stdout lines of "path\tsize\tmtime", or None for timeout
LISTINGS = {
    ("dropbox:", "", False): ["root.pdf\t10\t2020-01-02 03:04:05", "root.zip\t5\t2020-01-02 03:04:05", "x.txt\t1\t2020-01-02 03:04:05"],
    ("dropbox:Papers", "", True): ["a/one.pdf\t100\t2019-05-06 07:08:09", "b.docx\t200\tbad-time", "c.tgz\t3\t2021-01-01 00:00:00"],
    ("dropbox:Big", "", True): None,
    ("dropbox:Big/Sub1", "", True): ["deep.xlsx\t7\t2022-02-02 02:02:02"],
    ("dropbox:Big/Sub2", "", True): None,
    ("dropbox:Zotero", "", True): ["never.pdf\t1\t2020-01-01 00:00:00"],
    ("dropbox:xlsx", "", True): ["never.pdf\t1\t2020-01-01 00:00:00"],
    ("googledrive:", "", False): ["g.epub\t9\t2018-01-01 00:00:00"],
    ("googledrive:Notes", "", True): ["n.html\t4\t2023-03-03 03:03:03", "dup.pdf\t4\t2023-03-03 03:03:03"],
}
DIRS = {"dropbox:": ["Papers/", "Big/", "Zotero/", "xlsx/"], "dropbox:Big": ["Sub1/", "Sub2/"], "dropbox:Big/Sub2": [],
        "googledrive:": ["Notes/"]}


def fake_run(cmd, capture_output, text, timeout):
    remote = cmd[2]
    if "--dirs-only" in cmd:
        out = DIRS.get(remote)
        if out is None:
            return subprocess.CompletedProcess(cmd, 1, "", "err")
        return subprocess.CompletedProcess(cmd, 0, "\n".join(out), "")
    recursive = "-R" in cmd
    key = (remote, "", recursive)
    if key not in LISTINGS or LISTINGS[key] is None:
        raise subprocess.TimeoutExpired(cmd, timeout)
    return subprocess.CompletedProcess(cmd, 0, "\n".join(LISTINGS[key]), "")


def _run(tmp_path, monkeypatch, capsys, argv):
    for name, fname in (("SWEEP_LOG", "sweep_run.log"), ("ARCHIVES_LOG", "arch.jsonl"), ("SKIPPED_LOG", "skip.jsonl"),
                        ("CHECKPOINT_FILE", "ckpt.jsonl"), ("DB_PATH", "cat.duckdb")):
        monkeypatch.setattr(sw, name, tmp_path / fname)
    con = duckdb.connect(str(tmp_path / "cat.duckdb"))
    con.execute("""create table documents(item_key varchar primary key, title varchar, path varchar, source varchar,
        category varchar, topic varchar, filetype varchar, year varchar, md5 varchar, is_duplicate boolean,
        duplicate_of varchar, mtime bigint, date_added timestamp, auto_topic varchar)""")
    con.execute("insert into documents(item_key, path) values ('E1', 'dup.pdf')")
    con.close()
    monkeypatch.setattr(sw.subprocess, "run", fake_run)
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["sweep_all_folders.py", *argv])
    sw.main()
    norm = lambda s: re.sub(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ", "TS ", s)  # noqa: E731
    con = duckdb.connect(str(tmp_path / "cat.duckdb"), read_only=True)
    rows = con.execute("select item_key,title,path,source,category,topic,filetype,year,mtime from documents order by 1").fetchall()
    con.close()
    read = lambda n: (tmp_path / n).read_text() if (tmp_path / n).exists() else None  # noqa: E731
    return json.loads(json.dumps({
        "stdout": norm(capsys.readouterr().out), "log": norm(read("sweep_run.log")), "rows": rows,
        "archives": read("arch.jsonl"), "skipped": read("skip.jsonl"), "checkpoint": read("ckpt.jsonl")}))


@pytest.mark.parametrize("name,argv", [("full", ["--fresh"]), ("dry", ["--fresh", "--dry-run"])])
def test_main_matches_original_behaviour(name, argv, tmp_path, monkeypatch, capsys):
    got = _run(tmp_path, monkeypatch, capsys, argv)
    assert got == json.loads(GOLDEN.read_text())[name]
