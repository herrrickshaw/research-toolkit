"""Tests for zotero_crud.py. urllib is faked; nothing touches a network or the repo's key file."""

import importlib
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def load(monkeypatch, tmp_path):
    """Import zotero_crud fresh under a given env; restore sys.modules afterwards."""
    saved = sys.modules.pop("zotero_crud", None)

    def _load(**env):
        for k in ("ZOTERO_MODE", "ZOTERO_API_BASE", "ZOTERO_LIBRARY_PATH", "ZOTERO_API_KEY"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        sys.modules.pop("zotero_crud", None)
        mod = importlib.import_module("zotero_crud")
        monkeypatch.setattr(mod, "KEY_FILE", str(tmp_path / "key.json"))
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        return mod

    yield _load
    sys.modules.pop("zotero_crud", None)
    if saved is not None:
        sys.modules["zotero_crud"] = saved


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(monkeypatch, mod, outcomes):
    """outcomes: list of FakeResp or exceptions, consumed in order. Returns the call log."""
    calls = []
    seq = list(outcomes)

    def urlopen(req, timeout=None):
        calls.append((req.full_url, req.get_method(), dict(req.header_items()), req.data, timeout))
        o = seq.pop(0)
        if isinstance(o, BaseException):
            raise o
        return o

    monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
    return calls


# ---- module configuration ---------------------------------------------------------

def test_local_mode_defaults(load):
    m = load()
    assert m.MODE == "local"
    assert m.BASE == "http://127.0.0.1:23119/api"
    assert m.LIBRARY_PATH == "users/0"


def test_local_mode_base_override(load):
    m = load(ZOTERO_API_BASE="http://x:1/api")
    assert m.BASE == "http://x:1/api"


def test_cloud_mode_requires_library_path(load):
    with pytest.raises(RuntimeError, match="ZOTERO_LIBRARY_PATH"):
        load(ZOTERO_MODE="cloud")


def test_cloud_mode_config(load):
    m = load(ZOTERO_MODE="cloud", ZOTERO_LIBRARY_PATH="users/42")
    assert m.BASE == "https://api.zotero.org"
    assert m.LIBRARY_PATH == "users/42"


# ---- _request ---------------------------------------------------------------------

def test_request_success_prefixes_base_and_sets_headers(load, monkeypatch):
    m = load()
    calls = fake_urlopen(monkeypatch, m, [FakeResp(200, b"hi", {"A": "b"})])
    out = m._request("POST", "/users/0/items", headers={"X-Test": "1"}, data=b"d", timeout=7)
    assert out == (200, {"A": "b"}, b"hi")
    url, method, hdrs, data, timeout = calls[0]
    assert url == "http://127.0.0.1:23119/api/users/0/items"
    assert method == "POST" and data == b"d" and timeout == 7
    assert hdrs["X-test"] == "1"


@pytest.mark.parametrize("url", ["http://other/x", "https://other/x"])
def test_request_absolute_url_not_prefixed(load, monkeypatch, url):
    m = load()
    calls = fake_urlopen(monkeypatch, m, [FakeResp(200, b"")])
    m._request("GET", url)
    assert calls[0][0] == url


def test_request_http_error_returns_code_and_body(load, monkeypatch):
    m = load()
    err = urllib.error.HTTPError("u", 412, "nope", {"H": "v"}, io.BytesIO(b"conflict"))
    calls = fake_urlopen(monkeypatch, m, [err])
    status, headers, body = m._request("GET", "/x")
    assert (status, body) == (412, b"conflict")
    assert headers["H"] == "v"
    assert len(calls) == 1  # HTTP errors are not retried


def test_request_retries_then_succeeds(load, monkeypatch):
    m = load()
    sleeps = []
    monkeypatch.setattr(m.time, "sleep", sleeps.append)
    calls = fake_urlopen(monkeypatch, m, [TimeoutError("t"), ConnectionError("c"), FakeResp(200, b"ok")])
    assert m._request("GET", "/x")[0] == 200
    assert len(calls) == 3
    assert sleeps == [3, 6]  # linear backoff 3*(attempt+1)


def test_request_gives_up_after_retries(load, monkeypatch):
    m = load()
    calls = fake_urlopen(monkeypatch, m, [urllib.error.URLError("down")] * 3)
    status, headers, body = m._request("GET", "/x")
    assert status is None and headers == {}
    assert b"down" in body
    assert len(calls) == 3  # retries=2 -> 3 attempts


def test_request_zero_retries_single_attempt(load, monkeypatch):
    m = load()
    calls = fake_urlopen(monkeypatch, m, [TimeoutError("t")])
    assert m._request("GET", "/x", retries=0)[0] is None
    assert len(calls) == 1


# ---- get_server_id / key handling -----------------------------------------------------

def test_get_server_id(load, monkeypatch):
    m = load()
    monkeypatch.setattr(m, "_request", lambda *a, **k: (200, {"Zotero-Server-ID": "srv"}, b""))
    assert m.get_server_id() == "srv"


def test_get_server_id_missing_header(load, monkeypatch):
    m = load()
    monkeypatch.setattr(m, "_request", lambda *a, **k: (200, {}, b""))
    assert m.get_server_id() is None


def test_cloud_key_requires_env(load):
    m = load(ZOTERO_MODE="cloud", ZOTERO_LIBRARY_PATH="users/1")
    with pytest.raises(RuntimeError, match="ZOTERO_API_KEY"):
        m.load_or_create_key()


def test_cloud_key_from_env(load):
    m = load(ZOTERO_MODE="cloud", ZOTERO_LIBRARY_PATH="users/1", ZOTERO_API_KEY="k-123")
    assert m.load_or_create_key() == "k-123"


def test_local_key_cached_file_used(load, monkeypatch):
    m = load()
    Path(m.KEY_FILE).write_text(json.dumps({"key": "cached"}))
    monkeypatch.setattr(m, "_request", lambda *a, **k: pytest.fail("must not call the API"))
    assert m.load_or_create_key() == "cached"


def test_local_key_empty_cached_value_reauthorizes(load, monkeypatch):
    m = load()
    Path(m.KEY_FILE).write_text(json.dumps({"key": ""}))
    seen = []

    def req(method, path, headers=None, data=None, timeout=60, retries=2):
        seen.append((method, path))
        if path.endswith("/items?limit=1"):
            return 200, {"Zotero-Server-ID": "S"}, b""
        return 200, {}, json.dumps({"key": "fresh"}).encode()

    monkeypatch.setattr(m, "_request", req)
    assert m.load_or_create_key() == "fresh"
    assert json.loads(Path(m.KEY_FILE).read_text()) == {"key": "fresh"}


def test_local_authorize_sends_app_name_and_server_id(load, monkeypatch):
    m = load()
    captured = {}

    def req(method, path, headers=None, data=None, timeout=60, retries=2):
        if path == "/local/authorize":
            captured.update(headers=headers, data=json.loads(data), timeout=timeout)
            return 200, {}, json.dumps({"key": "k"}).encode()
        return 200, {"Zotero-Server-ID": "SID"}, b""

    monkeypatch.setattr(m, "_request", req)
    m.load_or_create_key(app_name="myapp")
    assert captured["data"] == {"appName": "myapp"}
    assert captured["headers"]["Zotero-Server-ID"] == "SID"


def test_local_authorize_failure_raises_and_writes_nothing(load, monkeypatch):
    m = load()

    def req(method, path, headers=None, data=None, timeout=60, retries=2):
        if path == "/local/authorize":
            return 403, {}, b"denied"
        return 200, {"Zotero-Server-ID": "S"}, b""

    monkeypatch.setattr(m, "_request", req)
    with pytest.raises(RuntimeError, match=r"authorize failed \(403\)"):
        m.load_or_create_key()
    assert not Path(m.KEY_FILE).exists()


# ---- ZoteroCRUD -------------------------------------------------------------------

@pytest.fixture
def zc(load, monkeypatch):
    m = load()
    Path(m.KEY_FILE).write_text(json.dumps({"key": "K"}))
    monkeypatch.setattr(m, "_request", lambda *a, **k: (200, {"Zotero-Server-ID": "SID"}, b""))
    return m, m.ZoteroCRUD()


def test_headers_local_include_server_id(zc):
    m, c = zc
    assert c._headers() == {"Zotero-API-Key": "K", "Zotero-Server-ID": "SID"}


def test_headers_extra_overrides_and_none_dropped(zc):
    m, c = zc
    h = c._headers({"Content-Type": "x", "Zotero-Server-ID": None})
    assert h == {"Zotero-API-Key": "K", "Content-Type": "x"}


def test_cloud_client_has_no_server_id(load):
    m = load(ZOTERO_MODE="cloud", ZOTERO_LIBRARY_PATH="users/1", ZOTERO_API_KEY="K")
    c = m.ZoteroCRUD()
    assert c.server_id is None
    assert c._headers() == {"Zotero-API-Key": "K"}


def test_create_items_connection_failure(zc, monkeypatch):
    m, c = zc
    monkeypatch.setattr(m, "_request", lambda *a, **k: (None, {}, "boom".encode()))
    assert c.create_items([{"a": 1}]) == (None, {"error": "boom"})


def test_create_items_empty_body(zc, monkeypatch):
    m, c = zc
    monkeypatch.setattr(m, "_request", lambda *a, **k: (200, {}, b""))
    assert c.create_items([]) == (200, {})


def test_create_items_malformed_body_is_a_failure_not_an_exception(zc, monkeypatch):
    m, c = zc
    monkeypatch.setattr(m, "_request", lambda *a, **k: (200, {}, b'{"success": '))
    status, resp = c.create_items([])
    assert status == 200
    assert "malformed response body" in resp["error"]
    assert resp.get("success", {}).get("0") is None  # the caller's check stays safe


def test_create_items_posts_json_to_library(zc, monkeypatch):
    m, c = zc
    seen = {}

    def req(method, path, headers=None, data=None, timeout=60, retries=2):
        seen.update(method=method, path=path, headers=headers, data=data, timeout=timeout)
        return 200, {}, json.dumps({"success": {"0": "KEY1"}}).encode()

    monkeypatch.setattr(m, "_request", req)
    status, resp = c.create_items([{"title": "t"}])
    assert (status, resp["success"]["0"]) == (200, "KEY1")
    assert seen["method"] == "POST" and seen["path"] == "/users/0/items"
    assert seen["headers"]["Content-Type"] == "application/json"
    assert json.loads(seen["data"]) == [{"title": "t"}]


@pytest.mark.parametrize("resp", [(None, b"x"), (200, b""), (200, b"not json")])
def test_get_collections_bad_responses_give_empty_list(zc, monkeypatch, resp):
    m, c = zc
    monkeypatch.setattr(m, "_request", lambda *a, **k: (resp[0], {}, resp[1]))
    assert c.get_collections() == []


def test_get_collections_parses(zc, monkeypatch):
    m, c = zc
    monkeypatch.setattr(m, "_request", lambda *a, **k: (200, {}, b'[{"key": "C"}]'))
    assert c.get_collections() == [{"key": "C"}]


# ---- upload_attachment_file ---------------------------------------------------------

class Script:
    """Scripted _request: returns/raises queued outcomes and records calls."""

    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, method, path, headers=None, data=None, timeout=60, retries=2):
        self.calls.append((method, path, headers, data, timeout))
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return o


@pytest.fixture
def upfile(tmp_path):
    p = tmp_path / "doc.bin"
    p.write_bytes(b"PAYLOAD")
    return p


def _auth(**kw):
    d = {"url": "https://s3/up", "uploadKey": "UK", "prefix": "", "suffix": ""}
    d.update(kw)
    return (200, {}, json.dumps(d).encode())


def test_upload_happy_path_three_steps(zc, upfile, monkeypatch):
    m, c = zc
    s = Script([_auth(), (201, {}, b""), (204, {}, b"")])
    monkeypatch.setattr(m, "_request", s)
    ok, status, body = c.upload_attachment_file("IK", str(upfile), "my doc.pdf", "application/pdf", "md5x", 123, 7)
    assert (ok, status) == (True, 204)
    reg, up, fin = s.calls
    assert reg[1] == "/users/0/items/IK/file"
    assert reg[3] == b"md5=md5x&filename=my%20doc.pdf&filesize=7&mtime=123"
    assert reg[2]["If-None-Match"] == "*"
    assert up[1] == "https://s3/up" and up[3] == b"PAYLOAD"
    assert up[2] == {"Content-Type": "application/pdf"}
    assert up[4] == 300  # default upload timeout
    assert fin[3] == b"upload=UK"


def test_upload_wraps_bytes_in_prefix_and_suffix(zc, upfile, monkeypatch):
    m, c = zc
    s = Script([_auth(prefix="--b\r\n", suffix="\r\n--b--", contentType="multipart/form-data; boundary=b"),
                (200, {}, b""), (204, {}, b"")])
    monkeypatch.setattr(m, "_request", s)
    assert c.upload_attachment_file("IK", str(upfile), "f", "application/pdf", "m", 1, 7)[0] is True
    assert s.calls[1][3] == b"--b\r\nPAYLOAD\r\n--b--"
    assert s.calls[1][2] == {"Content-Type": "multipart/form-data; boundary=b"}


def test_upload_custom_timeout_forwarded(zc, upfile, monkeypatch):
    m, c = zc
    s = Script([_auth(), (200, {}, b""), (204, {}, b"")])
    monkeypatch.setattr(m, "_request", s)
    c.upload_attachment_file("IK", str(upfile), "f", "t", "m", 1, 7, timeout=9)
    assert s.calls[1][4] == 9


def test_upload_authorize_failure_truncates_body(zc, upfile, monkeypatch):
    m, c = zc
    s = Script([(500, {}, b"x" * 1000)])
    monkeypatch.setattr(m, "_request", s)
    ok, status, body = c.upload_attachment_file("IK", str(upfile), "f", "t", "m", 1, 7)
    assert (ok, status) == (False, 500)
    assert len(body) == 300
    assert len(s.calls) == 1


@pytest.mark.parametrize("code", [200, 201, 204])
def test_upload_accepts_2xx_upload_statuses(zc, upfile, monkeypatch, code):
    m, c = zc
    monkeypatch.setattr(m, "_request", Script([_auth(), (code, {}, b""), (204, {}, b"")]))
    assert c.upload_attachment_file("IK", str(upfile), "f", "t", "m", 1, 7)[0] is True


def test_upload_bytes_step_failure(zc, upfile, monkeypatch):
    m, c = zc
    s = Script([_auth(), (403, {}, b"forbidden")])
    monkeypatch.setattr(m, "_request", s)
    assert c.upload_attachment_file("IK", str(upfile), "f", "t", "m", 1, 7) == (False, 403, b"forbidden")
    assert len(s.calls) == 2  # never registers an upload that failed


def test_upload_register_step_non_204_is_failure(zc, upfile, monkeypatch):
    m, c = zc
    monkeypatch.setattr(m, "_request", Script([_auth(), (200, {}, b""), (200, {}, b"odd")]))
    ok, status, _ = c.upload_attachment_file("IK", str(upfile), "f", "t", "m", 1, 7)
    assert (ok, status) == (False, 200)


def test_upload_retries_on_timeout_then_succeeds(zc, upfile, monkeypatch):
    m, c = zc
    sleeps = []
    monkeypatch.setattr(m.time, "sleep", sleeps.append)
    s = Script([TimeoutError("slow"), _auth(), (200, {}, b""), (204, {}, b"")])
    monkeypatch.setattr(m, "_request", s)
    assert c.upload_attachment_file("IK", str(upfile), "f", "t", "m", 1, 7)[0] is True
    assert sleeps == [5]


def test_upload_gives_up_after_retries(zc, upfile, monkeypatch):
    m, c = zc
    monkeypatch.setattr(m, "_request", Script([TimeoutError("a"), urllib.error.URLError("b")]))
    ok, status, msg = c.upload_attachment_file("IK", str(upfile), "f", "t", "m", 1, 7, retries=1)
    assert (ok, status) == (False, None)
    assert "b" in msg


def test_upload_zero_retries_single_attempt(zc, upfile, monkeypatch):
    m, c = zc
    s = Script([TimeoutError("a")])
    monkeypatch.setattr(m, "_request", s)
    assert c.upload_attachment_file("IK", str(upfile), "f", "t", "m", 1, 7, retries=0)[0] is False
    assert len(s.calls) == 1
