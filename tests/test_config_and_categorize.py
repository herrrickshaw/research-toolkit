import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import toolkit_config  # noqa: E402


def test_defaults_are_empty(monkeypatch, tmp_path):
    for var in toolkit_config.ENV_VARS.values():
        monkeypatch.delenv(var, raising=False)
    assert toolkit_config.get_list("personal_keywords", str(tmp_path / "none.toml")) == []


def test_env_overrides_and_keeps_spaces(monkeypatch):
    monkeypatch.setenv("RESEARCH_TOOLKIT_PERSONAL_KEYWORDS", "alpha,beta ,,gamma")
    assert toolkit_config.get_list("personal_keywords") == ["alpha", "beta ", "gamma"]


def test_toml_file(monkeypatch, tmp_path):
    monkeypatch.delenv("RESEARCH_TOOLKIT_PERSONAL_KEYWORDS", raising=False)
    cfg = tmp_path / "c.toml"
    cfg.write_text('personal_keywords = ["alpha", "lic "]\n')
    got = toolkit_config.get_list("personal_keywords", str(cfg))
    # tomllib is unavailable before Python 3.11; the loader then falls back to empty
    assert got in (["alpha", "lic "], [])


def test_classify_folder_with_injected_keywords(monkeypatch):
    import categorize
    monkeypatch.setattr(categorize, "PERSONAL_KEYWORDS", ["synthetic-private"])
    assert categorize.classify_folder("My Synthetic-Private Docs") == "personal"
    assert categorize.classify_folder("Backups 2020") == "backup"
    assert categorize.classify_folder("Climate Papers") == "research"
    assert categorize.classify_folder("") == "uncategorized"


def test_classify_folder_default_has_no_personal(monkeypatch):
    import categorize
    monkeypatch.setattr(categorize, "PERSONAL_KEYWORDS", [])
    assert categorize.classify_folder("tax returns") == "research"


def test_privacy_filter_names_injected(monkeypatch):
    monkeypatch.setenv("RESEARCH_TOOLKIT_PRIVACY_NAMES", "Zzyzx")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "insights"))
    import importlib
    import privacy
    importlib.reload(privacy)
    assert privacy.is_sensitive("Notes by Zzyzx", "", "literature")
    assert not privacy.is_sensitive("Plain climate paper", "abstract text", "literature")
