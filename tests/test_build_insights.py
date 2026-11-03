"""Characterisation test for insights/build_insights.py main().

Golden captured from the code BEFORE the pass-3 split, on a synthetic corpus in a temp dir.
Cluster-dependent output is only compared when scikit-learn matches the golden's version."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
sklearn = pytest.importorskip("sklearn")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "insights"))
import build_insights as bi  # noqa: E402
from insights_scenario import run_scenario  # noqa: E402

GOLDEN = Path(__file__).parent / "golden" / "build_insights_run.json"
CLUSTER_KEYS = ("stdout", "doc_clusters", "clusters", "near_dups", "memory_links", "report", "digest")


def test_main_matches_original_behaviour(tmp_path, monkeypatch, capsys):
    for name in ("OUT", "DB", "CATALOG", "MEMORY_DIR"):
        monkeypatch.setattr(bi, name, getattr(bi, name))
    got = run_scenario(bi, tmp_path, capsys=capsys)
    want = json.loads(GOLDEN.read_text())
    same_env = want["sklearn"] == sklearn.__version__
    for key, value in want["result"].items():
        if key in CLUSTER_KEYS and not same_env:
            continue
        assert got[key] == value, key
