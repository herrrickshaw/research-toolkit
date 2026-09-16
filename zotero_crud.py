"""
Thin client for Zotero's CRUD API - either the local desktop app's copy
(http://127.0.0.1:23119/api/, requires Settings > Advanced > Config Editor >
extensions.zotero.httpServer.localAPI.enabled = true) or the real cloud
Web API (https://api.zotero.org), selected via ZOTERO_MODE.

Local mode (default): explicit collection targeting and independently-
retryable attachment uploads that the connector's one-shot save endpoints
can't do well, and works even offline.

Cloud mode (ZOTERO_MODE=cloud): talks to zotero.org directly, so it keeps
working even when the local desktop app is closed, busy, or (as happened
once already) backlogged and unresponsive. Needs a real Web API key from
https://www.zotero.org/settings/keys (Settings > Feeds/API > Create new
private key, with library read/write) - set ZOTERO_API_KEY and
ZOTERO_LIBRARY_PATH (e.g. "users/21600525", found in that same items JSON
under library.id, or in any zotero.org URL for your library).
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

MODE = os.environ.get("ZOTERO_MODE", "local")
if MODE == "cloud":
    BASE = os.environ.get("ZOTERO_API_BASE", "https://api.zotero.org")
    LIBRARY_PATH = os.environ.get("ZOTERO_LIBRARY_PATH")
    if not LIBRARY_PATH:
        raise RuntimeError("ZOTERO_MODE=cloud requires ZOTERO_LIBRARY_PATH, e.g. 'users/21600525'")
else:
    BASE = os.environ.get("ZOTERO_API_BASE", "http://127.0.0.1:23119/api")
    LIBRARY_PATH = "users/0"

KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".zotero_api_key.json")


def _request(method, path, headers=None, data=None, timeout=60, retries=2):
    url = path if path.startswith("http://") or path.startswith("https://") else f"{BASE}{path}"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return resp.status, dict(resp.headers), body
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
                continue
            return None, {}, str(e).encode()


def get_server_id():
    """Local mode only - the cloud API has no equivalent concept."""
    status, headers, _ = _request("GET", f"/{LIBRARY_PATH}/items?limit=1")
    return headers.get("Zotero-Server-ID")


def load_or_create_key(app_name="dropbox-zotero-import"):
    if MODE == "cloud":
        key = os.environ.get("ZOTERO_API_KEY")
        if not key:
            raise RuntimeError("ZOTERO_MODE=cloud requires ZOTERO_API_KEY (create one at https://www.zotero.org/settings/keys)")
        return key
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
        self.server_id = get_server_id() if MODE != "cloud" else None

    def _headers(self, extra=None):
        h = {"Zotero-API-Key": self.key, "Zotero-Server-ID": self.server_id}
        if extra:
            h.update(extra)
        return {k: v for k, v in h.items() if v is not None}

    def create_items(self, items):
        """items: list of item dicts. Returns (status, parsed_json_or_{});
        status is None on a connection failure that survived retries - body
        is a raw error string in that case, not JSON, so it's never parsed.
        A 200 with a truthy-but-malformed body (seen under Zotero API load -
        a cut-off or garbage response despite the success status) is treated
        the same as a failed create rather than raising: the caller checks
        resp.get("success", {}).get("0"), which is safely None on {}."""
        status, _, body = _request(
            "POST", f"/{LIBRARY_PATH}/items",
            headers=self._headers({"Content-Type": "application/json"}),
            data=json.dumps(items).encode(),
            timeout=30,
        )
        if status is None:
            return None, {"error": body.decode("utf-8", "replace")}
        if not body:
            return status, {}
        try:
            return status, json.loads(body)
        except json.JSONDecodeError:
            return status, {"error": f"malformed response body: {body[:200]!r}"}

    def get_collections(self):
        status, _, body = _request("GET", f"/{LIBRARY_PATH}/collections", headers=self._headers())
        if status is None or not body:
            return []
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return []

    def upload_attachment_file(self, item_key, local_path, filename, content_type, md5, mtime_ms, filesize, retries=2, timeout=300):
        """Full 3-step attachment upload: authorize -> upload bytes -> register.
        Local mode's authorize response has empty prefix/suffix (raw bytes
        upload); the real cloud API's S3-backed authorize returns non-empty
        prefix/suffix that must sandwich the file bytes in a multipart-style
        envelope - both are handled the same way here since local's are
        just empty strings."""
        for attempt in range(retries + 1):
            try:
                status, _, body = _request(
                    "POST", f"/{LIBRARY_PATH}/items/{item_key}/file",
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
                    filedata = f.read()
                envelope = auth.get("prefix", "").encode() + filedata + auth.get("suffix", "").encode()
                status, _, body = _request(
                    "POST", auth["url"],
                    headers={"Content-Type": auth.get("contentType", content_type)},
                    data=envelope,
                    timeout=timeout,
                )
                if status not in (200, 201, 204):
                    return False, status, body[:300]

                status, _, body = _request(
                    "POST", f"/{LIBRARY_PATH}/items/{item_key}/file",
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
