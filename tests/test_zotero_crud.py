"""ZoteroCRUD against an in-process fake Zotero server on 127.0.0.1 (no real network)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

import zotero_crud


class FakeZotero(BaseHTTPRequestHandler):
    log: list = []
    authorize_status = 200
    upload_status = 201

    def log_message(self, *a):  # silence
        pass

    def _reply(self, status, body=b"", headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(200, b"[]", {"Zotero-Server-ID": "srv1"})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        FakeZotero.log.append((self.path, body))
        if self.path.endswith("/items"):
            self._reply(200, json.dumps({"success": {"0": "ABCD1234"}}).encode())
        elif self.path.endswith("/file") and body.startswith(b"md5="):
            if FakeZotero.authorize_status != 200:
                self._reply(FakeZotero.authorize_status, b"nope")
                return
            url = f"http://127.0.0.1:{self.server.server_port}/blob"
            self._reply(200, json.dumps({"url": url, "prefix": "<", "suffix": ">", "uploadKey": "UK"}).encode())
        elif self.path == "/blob":
            self._reply(FakeZotero.upload_status)
        elif self.path.endswith("/file") and body.startswith(b"upload="):
            self._reply(204)
        else:
            self._reply(404)


@pytest.fixture
def client(monkeypatch, tmp_path):
    server = HTTPServer(("127.0.0.1", 0), FakeZotero)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeZotero.log = []
    FakeZotero.authorize_status, FakeZotero.upload_status = 200, 201
    monkeypatch.setattr(zotero_crud, "BASE", f"http://127.0.0.1:{server.server_port}/api")
    monkeypatch.setattr(zotero_crud, "KEY_FILE", str(tmp_path / "key.json"))
    (tmp_path / "key.json").write_text(json.dumps({"key": "K"}))
    yield zotero_crud.ZoteroCRUD()
    server.shutdown()


def test_headers_drop_none_values(client):
    assert client._headers({"X": "1"}) == {"Zotero-API-Key": "K", "Zotero-Server-ID": "srv1", "X": "1"}


def test_create_items_parses_success(client):
    status, resp = client.create_items([{"itemType": "document"}])
    assert status == 200 and resp["success"]["0"] == "ABCD1234"


def test_upload_attachment_three_steps(client, tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"PDFDATA")
    ok, status, _ = client.upload_attachment_file("ITEM", str(f), "a.pdf", "application/pdf", "m5", 123, 7)
    assert (ok, status) == (True, 204)
    blob = [b for p, b in FakeZotero.log if p == "/blob"][0]
    assert blob == b"<PDFDATA>"
    authorize = [b for p, b in FakeZotero.log if b.startswith(b"md5=")][0]
    assert parse_qs(authorize.decode()) == {"md5": ["m5"], "filename": ["a.pdf"], "filesize": ["7"], "mtime": ["123"]}


def test_upload_stops_at_authorize_failure(client, tmp_path):
    FakeZotero.authorize_status = 412
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x")
    assert client.upload_attachment_file("ITEM", str(f), "a.pdf", "application/pdf", "m", 1, 1) == (False, 412, b"nope")
    assert not any(p == "/blob" for p, _ in FakeZotero.log)


def test_upload_reports_byte_upload_failure(client, tmp_path):
    FakeZotero.upload_status = 500
    f = tmp_path / "a.pdf"
    f.write_bytes(b"x")
    ok, status, _ = client.upload_attachment_file("ITEM", str(f), "a.pdf", "application/pdf", "m", 1, 1)
    assert (ok, status) == (False, 500)


def test_request_returns_none_status_on_connection_failure(monkeypatch):
    monkeypatch.setattr(zotero_crud.time, "sleep", lambda s: None)
    status, headers, body = zotero_crud._request("GET", "http://127.0.0.1:9/x", retries=1, timeout=1)
    assert status is None and headers == {} and body
