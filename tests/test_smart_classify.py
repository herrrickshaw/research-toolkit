"""Tests for smart_classify.py (LLM title classifier). urllib and Zotero are faked; the key is a dummy."""

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fakezotero import FakeCRUD, FakeZotero, item  # noqa: E402

import smart_classify as sc  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CLASSIFY_LOG_FILE", str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(sc, "CREDENTIALS_FILE", str(tmp_path / "creds.env"))
    return tmp_path


def logs(env):
    p = env / "log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---- load_key ---------------------------------------------------------------------

def test_load_key_missing_file(env):
    assert sc.load_key("GROQ_API_KEY") is None


@pytest.mark.parametrize("line,expected", [
    ("GROQ_API_KEY=abc", "abc"),
    ('GROQ_API_KEY="abc"', "abc"),
    ("GROQ_API_KEY='abc'", "abc"),
    ("  GROQ_API_KEY = x", None),            # spaces around '=' do not match the strict prefix
    ("GROQ_API_KEY=a=b", "a=b"),             # only the first '=' splits
    ("OTHER_GROQ_API_KEY=zzz", None),        # prefix must be at line start
])
def test_load_key_parsing(env, line, expected):
    (env / "creds.env").write_text("# c\nFOO=1\n" + line + "\n")
    assert sc.load_key("GROQ_API_KEY") == expected


def test_load_key_first_match_wins_and_unrelated_keys_ignored(env):
    (env / "creds.env").write_text("GROQ_API_KEY_OLD=old\nGROQ_API_KEY=new\nGROQ_API_KEY=later\n")
    assert sc.load_key("GROQ_API_KEY") == "new"


# ---- classify_title -------------------------------------------------------------------

class Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def chat(content):
    return {"choices": [{"message": {"content": content}}]}


def patch_urlopen(monkeypatch, result):
    seen = {}

    def urlopen(req, timeout=None):
        seen.update(url=req.full_url, method=req.get_method(), headers=dict(req.header_items()),
                    body=json.loads(req.data), timeout=timeout)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(sc.urllib.request, "urlopen", urlopen)
    return seen


def test_classify_title_request_shape(monkeypatch):
    seen = patch_urlopen(monkeypatch, Resp(chat('{"topic": "Solar power", "category": "research"}')))
    out = sc.classify_title("Solar Report 2024", "dummy-key")
    assert out == ({"topic": "Solar power", "category": "research"}, None)
    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["method"] == "POST" and seen["timeout"] == 30
    assert seen["headers"]["Authorization"] == "Bearer dummy-key"
    b = seen["body"]
    assert b["model"] == sc.MODEL and b["temperature"] == 0 and b["max_tokens"] == 250
    assert 'Title: "Solar Report 2024"' in b["messages"][0]["content"]


def test_classify_title_normalises_category_case_and_whitespace(monkeypatch):
    patch_urlopen(monkeypatch, Resp(chat('noise {"topic": "  Tax  ", "category": " PERSONAL "} trailing')))
    assert sc.classify_title("t", "k") == ({"topic": "Tax", "category": "personal"}, None)


def test_classify_title_json_spread_over_lines(monkeypatch):
    patch_urlopen(monkeypatch, Resp(chat('```json\n{\n "topic": "X",\n "category": "backup"\n}\n```')))
    assert sc.classify_title("t", "k")[0] == {"topic": "X", "category": "backup"}


@pytest.mark.parametrize("content,prefix", [
    ("no braces here", "no-json-in-response: no braces here"),
    ("{not json}", "bad-json: {not json}"),
    ('{"topic": "X", "category": "weird"}', "invalid-fields:"),
    ('{"topic": "", "category": "research"}', "invalid-fields:"),
    ('{"topic": "   ", "category": "research"}', "invalid-fields:"),
    ('{"category": "research"}', "invalid-fields:"),
    ('{"topic": "X"}', "invalid-fields:"),
])
def test_classify_title_failures(monkeypatch, content, prefix):
    patch_urlopen(monkeypatch, Resp(chat(content)))
    res, err = sc.classify_title("t", "k")
    assert res is None and err.startswith(prefix)


def test_classify_title_all_valid_categories_accepted(monkeypatch):
    for cat in sorted(sc.VALID_CATEGORIES):
        patch_urlopen(monkeypatch, Resp(chat(json.dumps({"topic": "T", "category": cat}))))
        assert sc.classify_title("t", "k")[0]["category"] == cat


def test_classify_title_missing_choices_key(monkeypatch):
    patch_urlopen(monkeypatch, Resp({}))
    res, err = sc.classify_title("t", "k")
    assert res is None and err.startswith("no-json-in-response")


@pytest.mark.xfail(strict=True, raises=IndexError,
                   reason="smart_classify.py:83 data.get('choices', [{}])[0] only defaults a MISSING key; "
                          "an empty choices list raises IndexError instead of returning (None, error)")
def test_classify_title_empty_choices_list(monkeypatch):
    patch_urlopen(monkeypatch, Resp({"choices": []}))
    res, err = sc.classify_title("t", "k")
    assert res is None and err.startswith("no-json-in-response")


def test_classify_title_error_content_truncated_to_200(monkeypatch):
    patch_urlopen(monkeypatch, Resp(chat("x" * 500)))
    assert len(sc.classify_title("t", "k")[1]) == len("no-json-in-response: ") + 200


def test_classify_title_http_error(monkeypatch):
    err = urllib.error.HTTPError("u", 429, "slow", {}, io.BytesIO(b"y" * 500))
    patch_urlopen(monkeypatch, err)
    res, msg = sc.classify_title("t", "k")
    assert res is None and msg == "http-429: " + "y" * 200


def test_classify_title_network_error(monkeypatch):
    patch_urlopen(monkeypatch, TimeoutError("timed out"))
    assert sc.classify_title("t", "k") == (None, "timed out")


# ---- tag helpers --------------------------------------------------------------------

def test_already_classified():
    assert sc.already_classified([{"tag": "ai-classified:true"}])
    assert not sc.already_classified([{"tag": "ai-classified:false"}, {"tag": "x"}])


def test_merge_tags_replaces_own_prefixes():
    existing = [{"tag": "category:uncategorized"}, {"tag": "topic:old"}, {"tag": "ai-classified:true"},
                {"tag": "source:dropbox"}, {"tag": "year:2020"}]
    out = sc.merge_tags(existing, "New topic", "research")
    assert [t["tag"] for t in out] == ["source:dropbox", "year:2020", "category:research", "topic:New topic",
                                       "ai-classified:true"]


# ---- fetch_uncategorized ------------------------------------------------------------

def test_fetch_uncategorized_filters_and_skips_classified(env, monkeypatch):
    items = [
        item("A", 1, title="a", url="u", tags=["category:uncategorized"]),
        item("B", 1, title="b", tags=["category:uncategorized", "ai-classified:true"]),
        item("C", 1, title="c", tags=["category:research"]),
        item("D", 1, tags=["category:uncategorized"]),
    ]
    FakeZotero(items).install(monkeypatch, sc)
    got = sc.fetch_uncategorized(FakeCRUD())
    assert [(g["key"], g["title"], g["url"]) for g in got] == [("A", "a", "u"), ("D", "", "")]


def test_fetch_uncategorized_paginates(env, monkeypatch):
    items = [item(f"K{i}", 1, tags=["category:uncategorized"]) for i in range(230)]
    FakeZotero(items).install(monkeypatch, sc)
    assert len(sc.fetch_uncategorized(FakeCRUD())) == 230


def test_fetch_uncategorized_gives_up_but_returns_partial(env, monkeypatch, capsys):
    items = [item(f"K{i}", 1, tags=["category:uncategorized"]) for i in range(150)]
    fz = FakeZotero(items).install(monkeypatch, sc)
    fz.override(lambda m, p: "start=100" in p, [(500, b"")] * 10)
    assert len(sc.fetch_uncategorized(FakeCRUD())) == 100
    assert "LIST-FAIL" in capsys.readouterr().out


def test_fetch_uncategorized_malformed_then_ok(env, monkeypatch):
    fz = FakeZotero([item("A", 1, tags=["category:uncategorized"])]).install(monkeypatch, sc)
    fz.override(lambda m, p: True, [(200, b"{bad")])
    assert len(sc.fetch_uncategorized(FakeCRUD())) == 1


def test_fetch_uncategorized_empty_body_ends(env, monkeypatch):
    fz = FakeZotero([]).install(monkeypatch, sc)
    fz.override(lambda m, p: True, [(200, b"")])
    assert sc.fetch_uncategorized(FakeCRUD()) == []


def test_patch_tags(env, monkeypatch):
    fz = FakeZotero([item("A", 2)]).install(monkeypatch, sc)
    assert sc.patch_tags(FakeCRUD(), "A", 2, [{"tag": "t"}]) == 204
    assert sc.patch_tags(FakeCRUD(), "A", 2, [{"tag": "t"}]) == 412


# ---- main -----------------------------------------------------------------------------

def test_main_without_key_exits_1(env, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        sc.main()
    assert e.value.code == 1
    assert "No GROQ_API_KEY" in capsys.readouterr().err


def test_main_classifies_falls_back_and_logs(env, monkeypatch, capsys):
    (env / "creds.env").write_text("GROQ_API_KEY=dummy\n")
    items = [
        item("A", 1, title="Good title", url="dropbox://x/a.pdf", tags=["category:uncategorized"]),
        item("B", 1, title="", url="dropbox://x/scan123.pdf", tags=["category:uncategorized", "topic:old"]),
    ]
    fz = FakeZotero(items).install(monkeypatch, sc)
    calls = []

    def fake_classify(title, key):
        calls.append((title, key))
        if title == "Good title":
            return {"topic": "Energy", "category": "research"}, None
        return None, "bad-json: x"

    monkeypatch.setattr(sc, "classify_title", fake_classify)
    sc.main()
    assert calls == [("Good title", "dummy"), ("scan123.pdf", "dummy")]   # empty title -> url basename
    a = [t["tag"] for t in fz.items["A"]["tags"]]
    assert a == ["category:research", "topic:Energy", "ai-classified:true"]
    b = [t["tag"] for t in fz.items["B"]["tags"]]
    assert b == ["category:uncategorized", "topic:unclear", "ai-classified:true"]
    statuses = [(r["key"], r["status"]) for r in logs(env)]
    assert statuses == [("A", "ok"), ("B", "fallback"), ("B", "ok")]
    assert "classified=2 failed=0" in capsys.readouterr().out
    assert "dummy" not in "".join(json.dumps(r) for r in logs(env))  # key never logged


def test_main_patch_failure_counted_and_logged(env, monkeypatch, capsys):
    (env / "creds.env").write_text("GROQ_API_KEY=dummy\n")
    fz = FakeZotero([item("A", 1, title="t", tags=["category:uncategorized"])]).install(monkeypatch, sc)
    fz.override(lambda m, p: m == "PATCH", [(500, b"")])
    monkeypatch.setattr(sc, "classify_title", lambda t, k: ({"topic": "T", "category": "research"}, None))
    sc.main()
    assert "classified=0 failed=1" in capsys.readouterr().out
    rec = logs(env)[-1]
    assert rec["status"] == "fail" and rec["error"] == "patch-500"


def test_main_skips_already_classified_items(env, monkeypatch, capsys):
    (env / "creds.env").write_text("GROQ_API_KEY=dummy\n")
    FakeZotero([item("A", 1, title="t", tags=["category:uncategorized", "ai-classified:true"])]).install(monkeypatch, sc)
    monkeypatch.setattr(sc, "classify_title", lambda t, k: pytest.fail("must not classify"))
    sc.main()
    assert "Found 0 items to classify" in capsys.readouterr().out
