"""
Thin client for Zotero 10's local CRUD API (http://127.0.0.1:23119/api/),
used for cases the connector's one-shot save endpoints can't handle well:
explicit collection targeting, and independently-retryable attachment uploads.

Requires: Settings > Advanced > Config Editor > extensions.zotero.httpServer.localAPI.enabled = true
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:23119/api"
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".zotero_api_key.json")


def _request(method, path, headers=None, data=None, timeout=60):
    url = f"{BASE}{path}"
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, dict(resp.headers), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get_server_id():
    status, headers, _ = _request("GET", "/users/0/items?limit=1")
    return headers.get("Zotero-Server-ID")


def load_or_create_key(app_name="dropbox-zotero-import"):
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            cached = json.load(f)
        if cached.get("key"):
            return cached["key"]
    server_id = get_server_id()
    status, _, body = _request(
        "POST", "/local/authorize",
        headers={"Content-Type": "application/json", "Zotero-Server-ID": server_id},
        data=json.dumps({"appName": app_name}).encode(),
        timeout=60,
    )
    if status != 200:
        raise RuntimeError(f"authorize failed ({status}): {body[:300]}")
    result = json.loads(body)
    with open(KEY_FILE, "w") as f:
        json.dump(result, f)
    return result["key"]


class ZoteroCRUD:
    def __init__(self):
        self.key = load_or_create_key()
        self.server_id = get_server_id()

    def _headers(self, extra=None):
        h = {"Zotero-API-Key": self.key, "Zotero-Server-ID": self.server_id}
        if extra:
            h.update(extra)
        return h

    def create_items(self, items):
        """items: list of item dicts. Returns parsed JSON response."""
        status, _, body = _request(
            "POST", "/users/0/items",
            headers=self._headers({"Content-Type": "application/json"}),
            data=json.dumps(items).encode(),
            timeout=30,
        )
        return status, json.loads(body) if body else {}

    def get_collections(self):
        status, _, body = _request("GET", "/users/0/collections", headers=self._headers())
        return json.loads(body) if body else []

    def upload_attachment_file(self, item_key, local_path, filename, content_type, md5, mtime_ms, filesize, retries=2, timeout=300):
        """Full 3-step attachment upload: authorize -> PUT bytes -> register."""
        for attempt in range(retries + 1):
            try:
                status, _, body = _request(
                    "POST", f"/users/0/items/{item_key}/file",
                    headers=self._headers({
                        "Content-Type": "application/x-www-form-urlencoded",
                        "If-None-Match": "*",
                    }),
                    data=(
                        f"md5={md5}&filename={urllib.parse.quote(filename)}"
                        f"&filesize={filesize}&mtime={mtime_ms}"
                    ).encode(),
                    timeout=30,
                )
                if status != 200:
                    return False, status, body[:300]
                auth = json.loads(body)

                with open(local_path, "rb") as f:
                    data = f.read()
                status, _, body = _request(
                    "POST", auth["url"].replace(BASE, ""),
                    headers={"Content-Type": auth.get("contentType", content_type)},
                    data=data,
                    timeout=timeout,
                )
                if status not in (200, 201, 204):
                    return False, status, body[:300]

                status, _, body = _request(
                    "POST", f"/users/0/items/{item_key}/file",
                    headers=self._headers({
                        "Content-Type": "application/x-www-form-urlencoded",
                        "If-None-Match": "*",
                    }),
                    data=f"upload={auth['uploadKey']}".encode(),
                    timeout=30,
                )
                return status == 204, status, body[:300]
            except (TimeoutError, urllib.error.URLError) as e:
                if attempt < retries:
                    time.sleep(5 * (attempt + 1))
                    continue
                return False, None, str(e)
