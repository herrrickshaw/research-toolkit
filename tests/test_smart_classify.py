"""smart_classify.py with the Groq call and Zotero HTTP layer faked (no network, no credentials)."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

import smart_classify as sc


def reply(content):
    return {"choices": [{"message": {"content": content}}]}


def test_classify_title_ok_and_normalises_category():
    chat = lambda body, key: reply('noise {"topic": " Tax Filing ", "category": "Personal"} tail')  # noqa: E731
    assert sc.classify_title("ITR 2020", "k", chat=chat) == ({"topic": "Tax Filing", "category": "personal"}, None)


@pytest.mark.parametrize("content,prefix", [
    ("no braces here", "no-json-in-response"),
    ("{not json}", "bad-json"),
    ('{"topic": "x", "category": "bogus"}', "invalid-fields"),
    ('{"topic": "", "category": "research"}', "invalid-fields"),
])
def test_parse_classification_errors(content, prefix):
    result, err = sc.parse_classification(content)
    assert result is None and err.startswith(prefix)


def test_classify_title_sends_prompt_and_key():
    seen = {}

    def chat(body, key):
        seen.update(body=json.loads(body), key=key)
        return reply('{"topic": "a", "category": "research"}')

    sc.classify_title("My Title", "SECRET", chat=chat)
    assert seen["key"] == "SECRET" and seen["body"]["model"] == sc.MODEL
    assert 'Title: "My Title"' in seen["body"]["messages"][0]["content"]


def test_classify_title_http_error_and_transport_error():
    def http_err(body, key):
        raise urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(b"slow down"))

    assert sc.classify_title("t", "k", chat=http_err) == (None, "http-429: slow down")

    def boom(body, key):
        raise ConnectionError("down")

    assert sc.classify_title("t", "k", chat=boom) == (None, "down")


def test_merge_tags_replaces_managed_prefixes_only():
    existing = [{"tag": "category:uncategorized"}, {"tag": "topic:Pdf"}, {"tag": "year:2020"}]
    assert sc.merge_tags(existing, "Tax", "personal") == [
        {"tag": "year:2020"}, {"tag": "category:personal"}, {"tag": "topic:Tax"}, {"tag": "ai-classified:true"}]
    assert sc.already_classified(sc.merge_tags([], "t", "research"))


def test_load_key(tmp_path):
    f = tmp_path / "cred.env"
    f.write_text('OTHER=1\nGROQ_API_KEY="abc123"\n')
    assert sc.load_key("GROQ_API_KEY", str(f)) == "abc123"
    assert sc.load_key("MISSING", str(f)) is None
    assert sc.load_key("GROQ_API_KEY", str(tmp_path / "nope.env")) is None


class FakeZC:
    def _headers(self, extra=None):
        return {"h": "1", **(extra or {})}


def item(key, tags=()):
    return {"data": {"key": key, "version": 3, "title": f"T{key}", "url": f"u{key}", "tags": list(tags)}}


def test_fetch_uncategorized_pages_retries_and_skips_classified(monkeypatch):
    monkeypatch.setattr(sc, "PAGE_SIZE", 2)
    responses = iter([
        (500, {}, b""),                                                  # retry
        (200, {}, json.dumps([item("a"), item("b", [{"tag": "ai-classified:true"}])]).encode()),
        (200, {}, json.dumps([item("c")]).encode()),
        (200, {}, b""),                                                  # empty body => end
    ])
    calls = []
    monkeypatch.setattr(sc, "_request", lambda *a, **k: (calls.append(a[1]), next(responses))[1])
    items = sc.fetch_uncategorized(FakeZC(), sleep=lambda s: None)
    assert [i["key"] for i in items] == ["a", "c"]
    assert "start=0" in calls[0] and "start=2" in calls[2]


def test_fetch_uncategorized_gives_up_after_retries(monkeypatch, capsys):
    monkeypatch.setattr(sc, "_request", lambda *a, **k: (500, {}, b""))
    assert sc.fetch_uncategorized(FakeZC(), sleep=lambda s: None) == []
    assert "[LIST-FAIL]" in capsys.readouterr().out


def test_main_patches_tags_and_logs(monkeypatch, tmp_path):
    log = tmp_path / "log.jsonl"
    monkeypatch.setattr(sc, "CLASSIFY_LOG_FILE", str(log))
    monkeypatch.setattr(sc, "load_key", lambda name, f=None: "k")
    monkeypatch.setattr(sc, "ZoteroCRUD", FakeZC)
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)
    monkeypatch.setattr(sc, "fetch_uncategorized", lambda zc: [
        {"key": "A", "version": 1, "title": "Good", "url": "", "tags": []},
        {"key": "B", "version": 2, "title": "Bad", "url": "", "tags": []}])
    monkeypatch.setattr(sc, "classify_title", lambda t, k: (
        ({"topic": "X", "category": "research"}, None) if t == "Good" else (None, "http-500")))
    patched = []
    monkeypatch.setattr(sc, "patch_tags", lambda zc, key, v, tags: (patched.append((key, tags)), 204)[1])
    sc.main()
    assert [k for k, _ in patched] == ["A", "B"]
    assert {"tag": "topic:unclear"} in patched[1][1]
    statuses = [json.loads(line)["status"] for line in log.read_text().splitlines()]
    assert sorted(statuses) == ["fallback", "ok", "ok"]
