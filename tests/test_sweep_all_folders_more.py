"""Further tests for sweep_all_folders.py: rclone wrappers, walk() failure branches, checkpointing,
DB connect retry and main(). rclone is faked at subprocess.run; the catalog is a tmp duckdb."""

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sweep_all_folders as sw  # noqa: E402

SCHEMA = """create table documents(item_key varchar primary key, title varchar, path varchar, source varchar,
  category varchar, topic varchar, filetype varchar, year varchar, md5 varchar, is_duplicate boolean,
  duplicate_of varchar, mtime bigint, date_added timestamp, auto_topic varchar)"""
MT = "2023-11-14 22:13:20"


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture
def env(tmp_path, monkeypatch):
    for name, fn in [("SWEEP_LOG", "sweep.log"), ("ARCHIVES_LOG", "arch.jsonl"), ("SKIPPED_LOG", "skip.jsonl"),
                     ("CHECKPOINT_FILE", "ckpt.jsonl"), ("DB_PATH", "cat.duckdb")]:
        monkeypatch.setattr(sw, name, tmp_path / fn)
    con = duckdb.connect(str(tmp_path / "cat.duckdb"))
    con.execute(SCHEMA)
    con.close()
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    return tmp_path


def jl(p):
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def docs(env):
    con = duckdb.connect(str(env / "cat.duckdb"), read_only=True)
    try:
        return con.execute("select item_key,title,path,source,category,topic,filetype,year,mtime from documents order by path").fetchall()
    finally:
        con.close()


# ---- constants / pure helpers ---------------------------------------------------------

def test_extension_sets_are_disjoint():
    assert not (sw.DOCUMENT_EXTENSIONS & sw.ARCHIVE_EXTENSIONS)
    assert {"pdf", "xlsx", "xls", "htm"} <= sw.DOCUMENT_EXTENSIONS
    assert sw.ARCHIVE_EXTENSIONS == {"zip", "tar", "gz", "tgz"}


def test_item_key_matches_discover_scripts():
    import discover_excel_files as d
    assert sw.item_key_for("dropbox", "a/b.pdf") == d.item_key_for("dropbox", "a/b.pdf")
    assert sw.item_key_for("dropbox", "a/b.pdf").startswith("XL_")


def test_mtime_ms_from_rclone():
    assert sw.mtime_ms_from_rclone(MT) == 1700000000000
    assert sw.mtime_ms_from_rclone("bad") is None


def test_log_prints_and_appends_with_timestamp(env, capsys):
    sw.log("hello")
    sw.log("again")
    lines = (env / "sweep.log").read_text().splitlines()
    assert len(lines) == 2 and lines[0].endswith(" hello") and lines[0][4] == "-"
    assert capsys.readouterr().out.count("hello") == 1


# ---- rclone wrappers ----------------------------------------------------------------------

def run_with(monkeypatch, handler):
    calls = []

    def run(cmd, capture_output=True, text=True, timeout=None):
        calls.append((list(cmd), timeout))
        r = handler(cmd)
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(sw.subprocess, "run", run)
    return calls


def test_rclone_lsd_strips_slashes_and_blank_lines(monkeypatch):
    calls = run_with(monkeypatch, lambda c: Proc(0, "A/\n\n  \nB\nC dir/\n"))
    assert sw.rclone_lsd("r:") == ["A", "B", "C dir"]
    assert calls[0] == ([sw.RCLONE_BIN, "lsf", "r:", "--dirs-only"], 60)


def test_rclone_lsd_failure_and_timeout_are_none(monkeypatch):
    run_with(monkeypatch, lambda c: Proc(1))
    assert sw.rclone_lsd("r:") is None
    run_with(monkeypatch, lambda c: subprocess.TimeoutExpired(c, 1))
    assert sw.rclone_lsd("r:") is None


def test_rclone_lsd_empty_listing_is_empty_list_not_none(monkeypatch):
    run_with(monkeypatch, lambda c: Proc(0, ""))
    assert sw.rclone_lsd("r:") == []


def test_rclone_lsf_recursive_parses_and_drops_malformed(monkeypatch):
    calls = run_with(monkeypatch, lambda c: Proc(0, f"a.pdf\t1\t{MT}\nbad\tonly-two\nx\ty\tz\textra\n"))
    assert sw.rclone_lsf_recursive("r:A", 33) == [("a.pdf", "1", MT)]
    cmd, timeout = calls[0]
    assert cmd[2] == "r:A" and "-R" in cmd and "--files-only" in cmd and timeout == 33


def test_rclone_lsf_recursive_failure_and_timeout_are_none(monkeypatch):
    run_with(monkeypatch, lambda c: Proc(2))
    assert sw.rclone_lsf_recursive("r:A", 1) is None
    run_with(monkeypatch, lambda c: subprocess.TimeoutExpired(c, 1))
    assert sw.rclone_lsf_recursive("r:A", 1) is None


# ---- walk() failure branches ------------------------------------------------------------------

def walk(monkeypatch, **fns):
    found, archives, skipped = [], [], []
    monkeypatch.setattr(sw, "rclone_lsf_recursive", fns["rec"])
    monkeypatch.setattr(sw, "rclone_lsd", fns.get("lsd", lambda *a, **k: []))
    if "run" in fns:
        monkeypatch.setattr(sw.subprocess, "run", fns["run"])
    sw.walk("r:", fns.get("folder", "A"), fns.get("depth", 0), fns.get("max_depth", 4), 10, found, archives, skipped)
    return found, archives, skipped


def test_walk_root_folder_path_empty_uses_remote_root(env, monkeypatch):
    seen = []
    found, _, _ = walk(monkeypatch, folder="", rec=lambda t, to: seen.append(t) or [("x.pdf", "1", MT)])
    assert seen == ["r:"] and found == [("x.pdf", "1", MT)]


def test_walk_skips_non_document_non_archive_files(env, monkeypatch):
    found, archives, _ = walk(monkeypatch, rec=lambda t, to: [("a.PDF", "1", MT), ("b.png", "1", MT), ("c.TGZ", "2", MT)])
    assert [f[0] for f in found] == ["A/a.PDF"]
    assert archives == [{"path": "A/c.TGZ", "size": "2", "mtime": MT}]


def test_walk_at_max_depth_is_skipped(env, monkeypatch):
    _, _, skipped = walk(monkeypatch, rec=lambda t, to: None, depth=4, max_depth=4)
    assert skipped == [{"path": "r:A", "reason": "timed out at max depth 4"}]


def test_walk_one_below_max_depth_still_subdivides(env, monkeypatch):
    calls = []

    def rec(t, to):
        calls.append(t)
        return None if t == "r:A" else [("f.pdf", "1", MT)]

    def run(cmd, capture_output=True, text=True, timeout=None):
        return Proc(0, "")

    found, _, skipped = walk(monkeypatch, rec=rec, lsd=lambda t, timeout=60: ["B"], run=run, depth=3, max_depth=4)
    assert [f[0] for f in found] == ["A/B/f.pdf"] and skipped == []


def test_walk_lsd_failure_is_skipped(env, monkeypatch):
    _, _, skipped = walk(monkeypatch, rec=lambda t, to: None, lsd=lambda t, timeout=60: None)
    assert skipped == [{"path": "r:A", "reason": "lsd (subfolder listing) itself failed"}]


def test_walk_no_subfolders_retries_with_triple_timeout_and_succeeds(env, monkeypatch):
    timeouts = []

    def rec(t, to):
        timeouts.append(to)
        return None if len(timeouts) == 1 else [("k.docx", "1", MT), ("z.zip", "2", MT)]

    found, archives, skipped = walk(monkeypatch, rec=rec)
    assert timeouts == [10, 30]
    assert [f[0] for f in found] == ["A/k.docx"] and [a["path"] for a in archives] == ["A/z.zip"]
    assert skipped == []


def test_walk_no_subfolders_retry_also_fails(env, monkeypatch):
    _, _, skipped = walk(monkeypatch, rec=lambda t, to: None)
    assert skipped == [{"path": "r:A", "reason": "no subfolders but still times out"}]


def test_walk_loose_file_listing_failure_recorded_but_recursion_continues(env, monkeypatch):
    def rec(t, to):
        return None if t == "r:A" else [("deep.pdf", "1", MT)]

    def run(cmd, capture_output=True, text=True, timeout=None):
        return Proc(1)

    found, _, skipped = walk(monkeypatch, rec=rec, lsd=lambda t, timeout=60: ["B"] if t == "r:A" else [], run=run)
    assert [f[0] for f in found] == ["A/B/deep.pdf"]
    assert skipped == [{"path": "r:A", "reason": "could not list files directly in this folder"}]


def test_walk_loose_file_listing_timeout_recorded(env, monkeypatch):
    def run(cmd, capture_output=True, text=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd, 1)

    _, _, skipped = walk(monkeypatch, rec=lambda t, to: None if t == "r:A" else [], lsd=lambda t, timeout=60: ["B"] if t == "r:A" else [],
                         run=run)
    assert skipped[0]["reason"] == "could not list files directly in this folder"


def test_walk_loose_files_classified_and_malformed_lines_ignored(env, monkeypatch):
    out = f"loose.pdf\t1\t{MT}\nloose.zip\t2\t{MT}\nloose.png\t3\t{MT}\nbroken line\n"

    def run(cmd, capture_output=True, text=True, timeout=None):
        return Proc(0, out)

    found, archives, _ = walk(monkeypatch, rec=lambda t, to: None if t == "r:A" else [], lsd=lambda t, timeout=60: ["B"] if t == "r:A" else [],
                              run=run)
    assert [f[0] for f in found] == ["A/loose.pdf"]
    assert [a["path"] for a in archives] == ["A/loose.zip"]


def test_walk_recurses_with_incremented_depth(env, monkeypatch):
    depths = []
    real_walk = sw.walk

    def spy(remote_root, folder_path, depth, *a, **k):
        depths.append((folder_path, depth))
        return real_walk(remote_root, folder_path, depth, *a, **k)

    monkeypatch.setattr(sw, "walk", spy)

    def run(cmd, capture_output=True, text=True, timeout=None):
        return Proc(0, "")

    found, archives, skipped = [], [], []
    monkeypatch.setattr(sw, "rclone_lsf_recursive", lambda t, to: None if t in ("r:A",) else [])
    monkeypatch.setattr(sw, "rclone_lsd", lambda t, timeout=60: ["B"] if t == "r:A" else [])
    monkeypatch.setattr(sw.subprocess, "run", run)
    spy("r:", "A", 0, 4, 10, found, archives, skipped)
    assert depths == [("A", 0), ("A/B", 1)]


# ---- connect_with_retry ---------------------------------------------------------------------

def test_connect_with_retry_succeeds_after_lock(env, monkeypatch):
    attempts = []
    real = duckdb.connect

    def connect(path, read_only=False):
        attempts.append(read_only)
        if len(attempts) < 3:
            raise duckdb.IOException("locked")
        return real(path, read_only=read_only)

    monkeypatch.setattr(sw.duckdb, "connect", connect)
    slept = []
    monkeypatch.setattr(sw.time, "sleep", slept.append)
    con = sw.connect_with_retry(read_only=True, poll_s=7)
    con.close()
    assert attempts == [True, True, True] and slept == [7, 7]


def test_connect_with_retry_gives_up_past_deadline(env, monkeypatch):
    def connect(path, read_only=False):
        raise duckdb.IOException("locked")

    monkeypatch.setattr(sw.duckdb, "connect", connect)
    with pytest.raises(duckdb.IOException):
        sw.connect_with_retry(read_only=False, max_wait_s=-1)


# ---- checkpoint ---------------------------------------------------------------------------------

def test_load_checkpoint_missing_file(env):
    assert sw.load_checkpoint() == (set(), [], [], [])


def test_checkpoint_roundtrip(env):
    sw.checkpoint("dropbox:", "Energy", "dropbox", [["a.pdf", "1", MT]], [{"path": "z.zip"}], [{"path": "r:x", "reason": "r"}])
    sw.checkpoint("dropbox:", "", "dropbox", [], [], [])
    with open(sw.CHECKPOINT_FILE, "a") as f:
        f.write("garbage\n")
    done, found, archives, skipped = sw.load_checkpoint()
    assert done == {("dropbox:", "Energy"), ("dropbox:", "")}
    assert found == [("dropbox", "a.pdf", "1", MT)]
    assert archives == [{"path": "z.zip"}] and skipped == [{"path": "r:x", "reason": "r"}]


# ---- main ---------------------------------------------------------------------------------------

class World:
    """Fake rclone universe: remote -> {'top': [dirs], 'root': [(file, size, mtime)], folder -> walk results}."""

    def __init__(self, monkeypatch, remotes):
        self.remotes, self.walked = remotes, []
        monkeypatch.setattr(sw, "rclone_lsd", lambda remote, timeout=60: self.remotes[remote].get("top"))
        monkeypatch.setattr(sw, "walk", self.walk)
        monkeypatch.setattr(sw.subprocess, "run", self.run)

    def run(self, cmd, capture_output=True, text=True, timeout=None):
        remote = cmd[2]
        spec = self.remotes[remote]
        if spec.get("root_timeout"):
            raise subprocess.TimeoutExpired(cmd, 1)
        return Proc(spec.get("root_rc", 0), "".join(f"{p}\t{s}\t{m}\n" for p, s, m in spec.get("root", [])))

    def walk(self, remote, folder, depth, max_depth, timeout, found, archives, skipped):
        self.walked.append((remote, folder, depth, max_depth, timeout))
        res = self.remotes[remote].get("walk", {}).get(folder, {})
        found.extend(res.get("found", []))
        archives.extend(res.get("archives", []))
        skipped.extend(res.get("skipped", []))


def run_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["sweep_all_folders.py", *argv])
    sw.main()


def test_main_end_to_end_inserts_and_writes_logs(env, monkeypatch, capsys):
    con = duckdb.connect(str(env / "cat.duckdb"))
    con.execute("insert into documents(item_key,path) values ('K','Energy/old.pdf')")
    con.close()
    w = World(monkeypatch, {
        "dropbox:": {"top": ["Energy", "pdf", "Zotero", "Law"],
                     "root": [("loose.pdf", "1", MT), ("notes.zip", "9", MT), ("pic.png", "1", MT)],
                     "walk": {"Energy": {"found": [("Energy/old.pdf", "1", MT), ("Energy/new.pdf", "1", MT)]},
                              "Law": {"archives": [{"path": "Law/a.zip", "size": "1", "mtime": MT}],
                                      "skipped": [{"path": "dropbox:Law/huge", "reason": "x"}]}}},
        "googledrive:": {"top": ["Drive Docs"], "walk": {"Drive Docs": {"found": [("Drive Docs/g.docx", "1", "bad-time")]}}},
    })
    run_main(monkeypatch)
    # 'pdf' is already-covered on dropbox only; Zotero is pathological; both are never walked
    assert [(r, f) for r, f, *_ in w.walked] == [("dropbox:", "Energy"), ("dropbox:", "Law"), ("googledrive:", "Drive Docs")]
    assert w.walked[0][2:] == (0, 4, 180)                      # depth, default max_depth, default timeout
    rows = {r[2]: r for r in docs(env)}
    assert set(rows) == {"Energy/old.pdf", "loose.pdf", "Energy/new.pdf", "Drive Docs/g.docx"}
    assert rows["Energy/new.pdf"][3:7] == ("dropbox", "research", "Energy", "pdf")
    assert rows["Drive Docs/g.docx"][3] == "googledrive" and rows["Drive Docs/g.docx"][8] is None
    assert rows["loose.pdf"][5] == "(root)" and rows["loose.pdf"][4] == "uncategorized"
    arch = jl(env / "arch.jsonl")
    assert sorted(a["path"] for a in arch) == ["Law/a.zip", "notes.zip"]
    assert {a["source"] for a in arch} == {"dropbox"}
    skipped = jl(env / "skip.jsonl")
    assert {"path": "dropbox:Law/huge", "reason": "x"} in skipped
    out = capsys.readouterr().out
    assert "new (not already catalogued): 3" in out and "inserted 3 new row(s) into documents (1 -> 4)" in out
    assert "scan complete: 4 document file(s)" in out


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="sweep_all_folders.py main(): a pathological-skip folder is checkpointed but never added to "
                          "all_skipped, so a fresh run omits it from sweep_skipped_folders.jsonl while a resumed run "
                          "(which reloads it from the checkpoint) includes it")
def test_main_pathological_skip_reported_in_skipped_log_on_fresh_run(env, monkeypatch):
    World(monkeypatch, {"dropbox:": {"top": ["Zotero"]}, "googledrive:": {"top": []}})
    run_main(monkeypatch, "--dry-run")
    assert {"path": "dropbox:Zotero", "reason": "pathological_skip"} in jl(env / "skip.jsonl")


def test_main_pathological_skip_is_reported_after_resume(env, monkeypatch):
    sw.checkpoint("dropbox:", "Zotero", "dropbox", [], [], [{"path": "dropbox:Zotero", "reason": "pathological_skip"}])
    World(monkeypatch, {"dropbox:": {"top": ["Zotero"]}, "googledrive:": {"top": []}})
    run_main(monkeypatch, "--dry-run")
    assert {"path": "dropbox:Zotero", "reason": "pathological_skip"} in jl(env / "skip.jsonl")


def test_main_dry_run_does_not_write_db_but_writes_scan_logs(env, monkeypatch):
    World(monkeypatch, {"dropbox:": {"top": ["Energy"], "walk": {"Energy": {"found": [("Energy/a.pdf", "1", MT)]}}},
                        "googledrive:": {"top": []}})
    run_main(monkeypatch, "--dry-run")
    assert docs(env) == []
    assert (env / "arch.jsonl").exists() and (env / "skip.jsonl").exists()
    assert "--dry-run: not writing to the database." in (env / "sweep.log").read_text()


def test_main_cli_args_forwarded_to_walk(env, monkeypatch):
    w = World(monkeypatch, {"dropbox:": {"top": ["Energy"]}, "googledrive:": {"top": []}})
    run_main(monkeypatch, "--dry-run", "--max-depth", "7", "--per-call-timeout", "5")
    assert w.walked == [("dropbox:", "Energy", 0, 7, 5)]


def test_main_resumes_from_checkpoint_without_rewalking(env, monkeypatch, capsys):
    sw.checkpoint("dropbox:", "", "dropbox", [["root.pdf", "1", MT]], [], [])
    sw.checkpoint("dropbox:", "Energy", "dropbox", [["Energy/a.pdf", "1", MT]], [], [])
    w = World(monkeypatch, {"dropbox:": {"top": ["Energy", "Law"], "root": [("SHOULD_NOT_APPEAR.pdf", "1", MT)]},
                            "googledrive:": {"top": []}})
    run_main(monkeypatch)
    assert [f for _, f, *_ in w.walked] == ["Law"]
    paths = {r[2] for r in docs(env)}
    assert paths == {"root.pdf", "Energy/a.pdf"}
    assert "resuming from checkpoint: 2 unit(s) already swept, 2 document(s)/0 archive(s)" in capsys.readouterr().out


def test_main_fresh_discards_checkpoint(env, monkeypatch):
    sw.checkpoint("dropbox:", "Energy", "dropbox", [["Energy/stale.pdf", "1", MT]], [], [])
    w = World(monkeypatch, {"dropbox:": {"top": ["Energy"]}, "googledrive:": {"top": []}})
    run_main(monkeypatch, "--fresh")
    assert [f for _, f, *_ in w.walked] == ["Energy"]
    assert docs(env) == []


def test_main_checkpoints_every_unit(env, monkeypatch):
    World(monkeypatch, {"dropbox:": {"top": ["Energy", "Zotero"]}, "googledrive:": {"top": ["D"]}})
    run_main(monkeypatch, "--dry-run")
    folders = [(r["remote"], r["folder"]) for r in jl(env / "ckpt.jsonl")]
    assert folders == [("dropbox:", ""), ("dropbox:", "Energy"), ("dropbox:", "Zotero"), ("googledrive:", ""), ("googledrive:", "D")]


def test_main_already_covered_only_applies_to_dropbox(env, monkeypatch):
    w = World(monkeypatch, {"dropbox:": {"top": ["PDF", "xlsx"]}, "googledrive:": {"top": ["pdf"]}})
    run_main(monkeypatch, "--dry-run")
    assert [(r, f) for r, f, *_ in w.walked] == [("googledrive:", "pdf")]


def test_main_root_listing_timeout_or_failure_is_tolerated(env, monkeypatch):
    World(monkeypatch, {"dropbox:": {"top": [], "root_timeout": True}, "googledrive:": {"top": [], "root_rc": 1}})
    run_main(monkeypatch, "--dry-run")
    assert [r["folder"] for r in jl(env / "ckpt.jsonl")] == ["", ""]


def test_main_unlistable_top_level_treated_as_no_folders(env, monkeypatch, capsys):
    World(monkeypatch, {"dropbox:": {"top": None}, "googledrive:": {"top": []}})
    run_main(monkeypatch, "--dry-run")
    assert "0 top-level folder(s) on dropbox:" in capsys.readouterr().out


def test_main_nothing_new_returns_before_insert(env, monkeypatch, capsys):
    con = duckdb.connect(str(env / "cat.duckdb"))
    con.execute("insert into documents(item_key,path) values ('K','Energy/a.pdf')")
    con.close()
    World(monkeypatch, {"dropbox:": {"top": ["Energy"], "walk": {"Energy": {"found": [("Energy/a.pdf", "1", MT)]}}},
                        "googledrive:": {"top": []}})
    run_main(monkeypatch)
    assert "new (not already catalogued): 0" in capsys.readouterr().out
    assert len(docs(env)) == 1


def test_main_is_idempotent_on_rerun(env, monkeypatch):
    spec = {"dropbox:": {"top": ["Energy"], "walk": {"Energy": {"found": [("Energy/a.pdf", "1", MT)]}}}, "googledrive:": {"top": []}}
    World(monkeypatch, spec)
    run_main(monkeypatch, "--fresh")
    World(monkeypatch, spec)
    run_main(monkeypatch, "--fresh")
    assert len(docs(env)) == 1
