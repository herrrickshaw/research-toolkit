"""Synthetic end-to-end scenario for pdf_to_markdown.run(), shared by the test
and by the golden-file generator. Uses only a temp dir and a fake rclone that
treats "remotes" as local directories - no network, no real library paths."""
from __future__ import annotations

import contextlib
import io
import json
import stat
from pathlib import Path

import duckdb

FAKE_RCLONE = """#!/usr/bin/env python3
import shutil, sys
from pathlib import Path
a = sys.argv[1:]
assert a[0] == "copy"
src, dst = Path(a[1]), Path(a[2])
dst.mkdir(parents=True, exist_ok=True)
if "--files-from" in a:
    for rel in Path(a[a.index("--files-from") + 1]).read_text().splitlines():
        if (src / rel).exists():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / rel, dst / rel)
else:
    for p in src.rglob("*"):
        if p.is_file():
            (dst / p.relative_to(src)).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst / p.relative_to(src))
"""


FAKE_MARKITDOWN = """#!/bin/sh
cat "$1"
"""

FAKE_SOFFICE = """#!/usr/bin/env python3
import sys
from pathlib import Path
a = sys.argv[1:]
flt = a[a.index("--convert-to") + 1]
out = Path(a[a.index("--outdir") + 1])
ext = ".csv" if flt.startswith("csv") else ".txt"
for f in a[a.index("--outdir") + 2:]:
    out.joinpath(Path(f).stem + ext).write_text("soffice text of " + Path(f).name)
"""


def _make_files(remote: Path) -> None:
    import docx
    import fitz
    import pptx

    remote.mkdir(parents=True)
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "Hello synthetic pdf")
    pdf.save(str(remote / "a.pdf"))
    pdf.close()
    blank = fitz.open()
    blank.new_page()
    blank.save(str(remote / "blank.pdf"))
    blank.close()
    d = docx.Document()
    d.add_paragraph("Docx paragraph one")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "c1"
    t.rows[0].cells[1].text = "c2"
    d.save(str(remote / "b.docx"))
    p = pptx.Presentation()
    s = p.slides.add_slide(p.slide_layouts[5])
    s.shapes.title.text = "Slide title"
    p.save(str(remote / "c.pptx"))
    (remote / "big.pdf").write_bytes(b"x" * (2 * 1024 * 1024))
    (remote / "e.html").write_text("<p>html body</p>")
    (remote / "f.doc").write_bytes(b"legacy doc")
    (remote / "g.xls").write_bytes(b"legacy xls")


def run_scenario(mod, tmp: Path) -> dict:
    remote = tmp / "remote"
    _make_files(remote)
    repo = tmp / "repo"
    repo.mkdir()
    rclone = tmp / "fake_rclone.py"
    rclone.write_text(FAKE_RCLONE)
    rclone.chmod(rclone.stat().st_mode | stat.S_IEXEC)

    mod.REPO_DIR = repo
    mod.DB_PATH = repo / "catalog.duckdb"
    mod.MD_OUTPUT_DIR = repo / "md_corpus"
    mod.STAGE_DIR = repo / "stage"
    mod.FETCH_DIR = mod.STAGE_DIR / "fetch"
    mod.UPLOAD_DIR = mod.STAGE_DIR / "upload"
    mod.RCLONE_BIN = str(rclone)
    mod.RCLONE_REMOTE_BY_SOURCE = {"dropbox": str(remote) + "/"}
    for name, body in (("markitdown", FAKE_MARKITDOWN), ("soffice", FAKE_SOFFICE)):
        f = tmp / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    mod.MARKITDOWN_BIN = str(tmp / "markitdown")
    mod.SOFFICE_BIN = str(tmp / "soffice")
    mod.MAX_SINGLE_FILE_MB = 1.0  # big.pdf (2 MB) trips the too_large branch

    con = duckdb.connect(str(mod.DB_PATH))
    con.execute("create table documents (item_key varchar, md5 varchar, title varchar, path varchar,"
                " source varchar, filetype varchar, is_duplicate boolean)")
    rows = [
        ("k1", None, "Alpha.pdf", "a.pdf", "dropbox", "pdf", False),
        ("k2", None, "Blank scan", "blank.pdf", "dropbox", "pdf", False),
        ("k3", None, "Beta", "b.docx", "dropbox", "docx", False),
        ("k4", None, "Gamma", "c.pptx", "dropbox", "pptx", False),
        ("k5", None, "Missing", "gone.pdf", "dropbox", "pdf", False),
        ("k6", None, "Inside zip", "x.zip!y.pdf", "dropbox", "pdf", False),
        ("k7", None, "Other source", "z.pdf", "onedrive", "pdf", False),
        ("k8", None, "Huge", "big.pdf", "dropbox", "pdf", False),
        ("k9", None, "Dup", "a.pdf", "dropbox", "pdf", True),
        ("ka", None, "Web page.html", "e.html", "dropbox", "html", False),
        ("kb", None, "Legacy doc", "f.doc", "dropbox", "doc", False),
        ("kc", None, "Legacy sheet", "g.xls", "dropbox", "xls", False),
    ]
    con.executemany("insert into documents values (?,?,?,?,?,?,?)", rows)
    con.close()

    out = {}
    for label, kwargs in (("first_run", {}), ("second_run", {}), ("status", {"status_only": True})):
        if label == "second_run":  # exercise retry_cloud_failures
            c = duckdb.connect(str(mod.DB_PATH))
            c.execute("update fulltext_md set cloud_status=NULL where item_key='k1'")
            c.close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.run(1000, 5000.0, **kwargs)
        text = buf.getvalue()
        if kwargs:  # --status lists equal-count groups in unspecified SQL order
            text = "\n".join(sorted(text.splitlines()))
        out[label + "_stdout"] = text

    con = duckdb.connect(str(mod.DB_PATH))
    out["rows"] = [
        [None if v is None else str(v) for v in r]
        for r in con.execute(
            "select item_key, title, source, catalog_path, md_path, status, chars, source_mb, error,"
            " cloud_status, cloud_pdf_path, cloud_md_path from fulltext_md order by item_key").fetchall()
    ]
    con.close()
    out["md_files"] = {p.name: p.read_text() for p in sorted(mod.MD_OUTPUT_DIR.glob("*.md"))}
    out["uploaded"] = sorted(str(p.relative_to(remote)) for p in remote.rglob("*")
                             if p.is_file() and "research_md_corpus" in str(p))
    out["stage_left"] = sorted(p.name for p in mod.STAGE_DIR.iterdir())
    text = json.dumps(out, sort_keys=True)
    return json.loads(text.replace(str(tmp), "<TMP>"))
