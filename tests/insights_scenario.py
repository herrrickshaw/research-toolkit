"""Synthetic corpus + catalog for the build_insights characterisation test (temp dir only)."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import duckdb

TOPICS = {
    "battery": "lithium cathode anode electrolyte charging cell voltage storage grid capacity cycle",
    "genome": "protein sequence genome expression cell mutation allele pathway enzyme receptor binding",
    "solar": "photovoltaic silicon module inverter irradiance panel efficiency tracker array wafer",
    "bank": "monetary inflation lending interest credit deposit liquidity regulation sovereign yield",
    "ocean": "salinity current plankton reef coastal tide fisheries sediment upwelling marine",
}
FILLER = "analysis results method study report evidence sample outcome review model parameter design"


def _body(topic: str, n: int, extra: str = "") -> str:
    words = TOPICS[topic].split()
    text = " ".join(words[(i * 3 + n) % len(words)] + " " + words[(i + n) % len(words)] for i in range(40))
    return f"Published 20{15 + n % 10}. {text} {FILLER} {extra}".strip() + " " + FILLER * 4


def build(root: Path) -> dict:
    """Write md files, a catalog duckdb and memory notes under root; return the doc list."""
    md, mem = root / "md", root / "memory"
    md.mkdir()
    mem.mkdir()
    docs = []
    for t_i, topic in enumerate(TOPICS):
        for n in range(9):
            key = f"T{t_i}{n:07d}"
            extra = ""
            if topic == "solar" and n == 0:
                extra = "The global panel market was valued at USD 5 billion in 2024 and is expected to grow."
            if topic == "solar" and n == 1:
                extra = "The global panel market is projected to reach USD 20 billion in 2024; CAGR of 7.5%."
            text = _body(topic, n, extra)
            if n == 8:  # exact copy of n == 7
                text = _body(topic, 7, "")
            docs.append(dict(key=key, title=f"{key}_{topic} study {n}", text=text, category="research",
                             path=f"dropbox:Papers/{topic}/{key}.pdf", year=str(2010 + n * 2) if n % 3 else "unknown"))
    docs.append(dict(key="OW000001", title="OW000001_own notes", text=_body("bank", 3, "market-pipeline daily scan report"),
                     category="backup", path="dropbox:Machine_Backup/x.md", year="2025"))
    docs.append(dict(key="SE000001", title="SE000001_bank statement jan", text=_body("bank", 2, "account statement"),
                     category="research", path="dropbox:Papers/x.pdf", year="2020"))
    docs.append(dict(key="PE000001", title="PE000001_private", text=_body("ocean", 2), category="personal",
                     path="dropbox:Papers/p.pdf", year="2020"))
    docs.append(dict(key="CV000001", title="CV000001_priv folder doc", text=_body("ocean", 4), category="research",
                     path="dropbox:Documents/p.pdf", year="2021"))
    con = duckdb.connect(str(root / "catalog.duckdb"))
    con.execute("create table fulltext_md(item_key varchar, title varchar, md_path varchar, chars bigint, "
                "converted_at timestamp, status varchar)")
    con.execute("create table documents(item_key varchar, title varchar, category varchar, auto_topic varchar, "
                "topic varchar, year varchar, filetype varchar, path varchar)")
    now = datetime.now()
    for d in docs:
        p = md / f"{d['key']}.md"
        p.write_text(d["text"], encoding="utf-8")
        con.execute("insert into fulltext_md values (?,?,?,?,?,'ok')", [d["key"], d["title"], str(p), len(d["text"]), now])
        con.execute("insert into documents values (?,?,?,?,?,?,?,?)",
                    [d["key"], d["title"], d["category"], None, d["path"].split("/")[1], d["year"], "pdf", d["path"]])
    con.close()
    (mem / "MEMORY.md").write_text("index", encoding="utf-8")
    (mem / "project_battery_notes.md").write_text(
        "---\nname: battery\ndescription: lithium cathode storage grid notes\n---\nbattery cell voltage capacity", encoding="utf-8")
    (mem / "feedback_api_token.md").write_text("---\ndescription: x\n---\nsecret", encoding="utf-8")
    return {"docs": docs}


def run_scenario(mod, root: Path, argv=("build_insights.py",), capsys=None) -> dict:
    """Run mod.main() against the synthetic tree and return normalised outputs."""
    build(root)
    out = root / "out"
    mod.OUT = out
    mod.DB = out / "research_insights.duckdb"
    mod.CATALOG = root / "catalog.duckdb"
    mod.MEMORY_DIR = root / "memory"
    old = sys.argv
    sys.argv = list(argv)
    try:
        mod.main()
    finally:
        sys.argv = old
    today = str(mod.date.today())
    norm = lambda s: s.replace(today, "TODAY").replace(str(root), "ROOT")  # noqa: E731
    stdout = norm(re.sub(r"\(\d+s\)", "(Ns)", capsys.readouterr().out)) if capsys else ""
    db = duckdb.connect(str(mod.DB if not old[1:] and "--limit" not in argv else out / "smoke.duckdb"), read_only=True)
    q = lambda s: [[str(c) for c in r] for r in db.execute(s).fetchall()]  # noqa: E731
    res = {
        "stdout": stdout,
        "docs": q("select item_key,title,category,topic,year,chars,text_hash,is_canonical,first_seen,kind,shareable from docs order by 1"),
        "doc_clusters": q("select item_key, cluster from docs order by 1"),
        "claims": q("select * from claims order by 1,3,4,6"),
        "claims_done": q("select * from claims_done order by 1"),
        "clusters": q("select * from clusters order by 1"),
        "near_dups": q("select * from near_dups order by 1,3"),
        "memory_links": q("select * from memory_links order by 1,3"),
        "contradictions": q("select * from contradictions order by 1,2,3"),
        "run_log": q("select docs, unique_docs, note from run_log"),
        "report": norm((out / "report_latest.md").read_text()),
        "digest": norm((out / f"digest_{today}.md").read_text()),
        "files": sorted(p.name.replace(today, "TODAY") for p in out.iterdir()),
    }
    db.close()
    return json.loads(json.dumps(res))
