"""Test support: an in-memory stand-in for the Zotero local API, used by the script tests.

Not a test module. It replaces each script's imported `_request` and `ZoteroCRUD`, so no socket is opened.
"""
import json
import urllib.parse


class FakeCRUD:
    key = "K"
    server_id = "S"

    def _headers(self, extra=None):
        h = {"Zotero-API-Key": "K", "Zotero-Server-ID": "S"}
        if extra:
            h.update(extra)
        return h


class FakeZotero:
    def __init__(self, items=(), collections=()):
        self.items = {d["key"]: dict(d) for d in items}
        self.collections = {c["key"]: dict(c) for c in collections}
        self.calls = []        # (method, path)
        self.patches = []      # (key, payload)
        self.overrides = []    # [predicate(method, path) -> bool, [responses...]]
        self._n = 0

    def override(self, predicate, responses):
        """The next len(responses) calls matching predicate return these (status, body) pairs
        (or raise, if an item is an exception)."""
        self.overrides.append([predicate, list(responses)])

    def request(self, method, path, headers=None, data=None, timeout=60, retries=2):
        self.calls.append((method, path))
        for pred, resp in self.overrides:
            if resp and pred(method, path):
                r = resp.pop(0)
                if isinstance(r, BaseException):
                    raise r
                status, body = r
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                return status, {}, body if isinstance(body, bytes) else body.encode()
        u = urllib.parse.urlparse(path)
        q = dict(urllib.parse.parse_qsl(u.query))
        parts = u.path.strip("/").split("/")
        if method == "GET" and parts[-1] == "items":
            vals = list(self.items.values())
            if "tag" in q:
                vals = [d for d in vals if any(t["tag"] == q["tag"] for t in d.get("tags", []))]
            start, limit = int(q.get("start", 0)), int(q.get("limit", 25))
            return 200, {}, json.dumps([{"data": d} for d in vals[start:start + limit]]).encode()
        if method == "GET" and parts[-1] == "collections":
            return 200, {}, json.dumps([{"data": c} for c in self.collections.values()]).encode()
        if method == "POST" and parts[-1] == "collections":
            out = {}
            for i, c in enumerate(json.loads(data)):
                self._n += 1
                key = f"COL{self._n}"
                self.collections[key] = {"key": key, **c}
                out[str(i)] = key
            return 200, {}, json.dumps({"success": out}).encode()
        if method == "PATCH" and parts[-2] == "items":
            key = parts[-1]
            d = self.items.get(key)
            if d is None:
                return 404, {}, b""
            if str((headers or {}).get("If-Unmodified-Since-Version")) != str(d["version"]):
                return 412, {}, b""
            payload = json.loads(data)
            self.patches.append((key, payload))
            d.update(payload)
            d["version"] += 1
            return 204, {}, b""
        return 404, {}, b""

    def install(self, monkeypatch, module):
        monkeypatch.setattr(module, "_request", self.request)
        if hasattr(module, "ZoteroCRUD"):
            monkeypatch.setattr(module, "ZoteroCRUD", FakeCRUD)
        monkeypatch.setattr(module.time, "sleep", lambda s: None)
        return self


def item(key, version=1, tags=(), **fields):
    d = {"key": key, "version": version, "tags": [{"tag": t} for t in tags]}
    d.update(fields)
    return d
