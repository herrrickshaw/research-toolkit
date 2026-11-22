"""Tests for remediate_failed.py: filename parsing, bibliographic lookups and the remediation loop.
Open Library / Google Books, rclone and Zotero are all faked."""

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import remediate_failed as rf  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(rf, "LOG_FILE", str(tmp_path / "log.jsonl"))
    monkeypatch.setattr(rf, "STAGING_DIR", str(stage))
    monkeypatch.setattr(rf, "DROPBOX_IMPORT_COLLECTION_KEY", "")
    monkeypatch.setattr(rf.time, "sleep", lambda s: None)
    return tmp_path


def recs(env):
    p = env / "log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---- load_log / log ---------------------------------------------------------------------

def test_load_log_missing(env):
    assert rf.load_log() == (set(), {})


def test_load_log_ok_overrides_earlier_fail_and_later_fail_does_not_override_ok(env):
    lines = [
        {"path": "a", "status": "fail", "stage": "download"},
        {"path": "a", "status": "ok"},
        {"path": "b", "status": "ok"},
        {"path": "b", "status": "fail", "stage": "late"},
        {"path": "c", "status": "fail", "stage": "one"},
        {"path": "c", "status": "fail", "stage": "two"},
    ]
    (env / "log.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    ok, fail = rf.load_log()
    assert ok == {"a", "b"}
    assert list(fail) == ["c"] and fail["c"]["stage"] == "two"   # latest failure wins


def test_log_appends(env):
    rf.log({"a": 1})
    rf.log({"a": 2})
    assert [r["a"] for r in recs(env)] == [1, 2]


# ---- parse_filename ------------------------------------------------------------------------

def test_parse_series_author_title_year_publisher():
    p = rf.parse_filename("(Series) John Smith - Some Title (2010)-Publisher.pdf")
    assert p == {"title": "Some Title", "year": "2010",
                 "authors": [{"lastName": "Smith", "firstName": "John", "creatorType": "author"}]}


def test_parse_multiple_authors_split_on_comma_and_semicolon():
    p = rf.parse_filename("Ann Lee, Bob Ray and Cy Day; Di Fox - Book.pdf")
    assert [a["lastName"] for a in p["authors"]] == ["Lee", "Ray", "Day", "Fox"]
    assert p["authors"][0]["firstName"] == "Ann"


def test_parse_single_name_author_has_empty_first_name():
    a = rf.parse_filename("Madonna - Hits.pdf")["authors"]
    assert a == [{"lastName": "Madonna", "firstName": "", "creatorType": "author"}]


def test_parse_et_al_alone_is_dropped():
    assert rf.parse_filename("Smith, et al. - Paper.pdf")["authors"] == [
        {"lastName": "Smith", "firstName": "", "creatorType": "author"}]
    assert rf.parse_filename("Smith, ET AL - Paper.pdf")["authors"][0]["lastName"] == "Smith"
    assert len(rf.parse_filename("Smith, ET AL - Paper.pdf")["authors"]) == 1


def test_parse_underscores_become_spaces_in_title():
    assert rf.parse_filename("Jane Doe - A_Fine__Title.epub")["title"] == "A Fine Title"


@pytest.mark.parametrize("name", ["plainname.pdf", "no dash here.epub"])
def test_parse_no_author_dash_falls_back_to_stem(name):
    p = rf.parse_filename(name)
    assert p == {"title": Path(name).stem, "authors": [], "year": None}


def test_parse_year_must_be_19xx_or_20xx():
    assert rf.parse_filename("A B - T (1999).pdf")["year"] == "1999"
    assert rf.parse_filename("A B - T (2024).pdf")["year"] == "2024"
    assert rf.parse_filename("A B - T (1850).pdf")["year"] is None
    assert rf.parse_filename("A B - T (2100).pdf")["year"] is None


# ---- lookups ----------------------------------------------------------------------------------

class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def patch_urlopen(monkeypatch, payload):
    seen = {}

    def urlopen(url, timeout=None):
        seen.update(url=url, timeout=timeout)
        if isinstance(payload, BaseException):
            raise payload
        return Resp(json.dumps(payload).encode())

    monkeypatch.setattr(rf.urllib.request, "urlopen", urlopen)
    return seen


def test_openlibrary_lookup_maps_fields_and_builds_query(env, monkeypatch):
    seen = patch_urlopen(monkeypatch, {"docs": [{
        "title": "T", "author_name": ["A B"], "first_publish_year": 1999,
        "publisher": ["P1", "P2"], "isbn": ["111", "222"]}]})
    out = rf.openlibrary_lookup("The Title", [{"lastName": "Smith", "firstName": "J"}])
    assert out == {"title": "T", "authors": ["A B"], "publisher": "P1", "date": "1999", "isbn": "111", "abstract": None}
    assert seen["url"].startswith("https://openlibrary.org/search.json?")
    assert "title=The+Title" in seen["url"] and "author=Smith" in seen["url"] and "limit=1" in seen["url"]
    assert seen["timeout"] == 15


def test_openlibrary_lookup_without_authors_omits_author_param(env, monkeypatch):
    seen = patch_urlopen(monkeypatch, {"docs": []})
    assert rf.openlibrary_lookup("T", []) is None
    assert "author=" not in seen["url"]


def test_openlibrary_lookup_sparse_doc(env, monkeypatch):
    patch_urlopen(monkeypatch, {"docs": [{"title": "T"}]})
    out = rf.openlibrary_lookup("T", [])
    assert out == {"title": "T", "authors": [], "publisher": None, "date": None, "isbn": None, "abstract": None}


@pytest.mark.parametrize("payload", [{}, {"docs": None}, {"docs": []}])
def test_openlibrary_lookup_no_docs(env, monkeypatch, payload):
    patch_urlopen(monkeypatch, payload)
    assert rf.openlibrary_lookup("T", []) is None


def test_openlibrary_lookup_failure_returns_none(env, monkeypatch, capsys):
    patch_urlopen(monkeypatch, TimeoutError("slow"))
    assert rf.openlibrary_lookup("T", []) is None
    assert "open library lookup failed: slow" in capsys.readouterr().out


def test_google_books_lookup_maps_fields_and_prefers_isbn13_or_10(env, monkeypatch):
    seen = patch_urlopen(monkeypatch, {"items": [{"volumeInfo": {
        "title": "GT", "authors": ["X Y"], "publisher": "GP", "publishedDate": "2001-05",
        "description": "desc",
        "industryIdentifiers": [{"type": "OTHER", "identifier": "zzz"}, {"type": "ISBN_10", "identifier": "123"},
                                {"type": "ISBN_13", "identifier": "9781"}]}}]})
    out = rf.google_books_lookup("The Title", [{"lastName": "Smith"}])
    assert out == {"title": "GT", "authors": ["X Y"], "publisher": "GP", "date": "2001-05", "isbn": "123", "abstract": "desc"}
    assert "q=intitle%3AThe+Title%2Binauthor%3ASmith" in seen["url"]
    assert "maxResults=1" in seen["url"]


def test_google_books_lookup_no_isbn_and_no_author(env, monkeypatch):
    seen = patch_urlopen(monkeypatch, {"items": [{"volumeInfo": {"industryIdentifiers": [{"type": "OTHER", "identifier": "q"}]}}]})
    out = rf.google_books_lookup("T", [])
    assert out["isbn"] is None and out["authors"] == [] and out["title"] is None
    assert "inauthor" not in seen["url"]


@pytest.mark.parametrize("payload", [{}, {"items": []}, {"items": None}])
def test_google_books_lookup_no_items(env, monkeypatch, payload):
    patch_urlopen(monkeypatch, payload)
    assert rf.google_books_lookup("T", []) is None


def test_google_books_lookup_failure(env, monkeypatch, capsys):
    patch_urlopen(monkeypatch, ConnectionError("x"))
    assert rf.google_books_lookup("T", []) is None
    assert "google books lookup failed" in capsys.readouterr().out


# ---- build_creators --------------------------------------------------------------------------

def test_build_creators_from_lookup_names():
    assert rf.build_creators(["Ann Marie Lee", "Plato"], [{"x": 1}]) == [
        {"creatorType": "author", "firstName": "Ann Marie", "lastName": "Lee"},
        {"creatorType": "author", "lastName": "Plato", "firstName": ""},
    ]


def test_build_creators_falls_back_when_lookup_has_none():
    fb = [{"lastName": "F"}]
    assert rf.build_creators([], fb) is fb
    assert rf.build_creators(None, fb) is fb


# ---- main -------------------------------------------------------------------------------------

class StubCRUD:
    def __init__(self, creates=None, upload=(True, 204, b"")):
        self.creates = list(creates or [(200, {"success": {"0": "BOOK"}}), (200, {"success": {"0": "ATT"}})])
        self.upload, self.created, self.uploaded = upload, [], []

    def create_items(self, items):
        self.created.append(items[0])
        return self.creates.pop(0)

    def upload_attachment_file(self, *a, **k):
        self.uploaded.append(a)
        return self.upload


def setup_main(env, monkeypatch, fails, crud, enriched="ol", dl_ok=True):
    (env / "log.jsonl").write_text("".join(json.dumps({"path": p, "status": "fail"}) + "\n" for p in fails))
    monkeypatch.setattr(rf, "ZoteroCRUD", lambda: crud)
    state = {"removed": []}

    def dl(path, local):
        if dl_ok:
            Path(local).write_bytes(b"BYTES")
        return dl_ok, ("" if dl_ok else "gone")

    monkeypatch.setattr(rf, "download", dl)
    ol = {"title": "OL Title", "authors": ["Real Author"], "publisher": "Pub", "date": "2005", "isbn": "isbn1", "abstract": None}
    monkeypatch.setattr(rf, "openlibrary_lookup", lambda t, a: ol if enriched == "ol" else None)
    monkeypatch.setattr(rf, "google_books_lookup",
                        lambda t, a: {"title": "GB Title", "authors": [], "publisher": None, "date": None, "isbn": None,
                                      "abstract": "abs"} if enriched == "gb" else None)
    return state


def staged(env):
    return list((env / "stage").iterdir())


def test_main_happy_path_with_open_library_metadata(env, monkeypatch, capsys):
    crud = StubCRUD()
    setup_main(env, monkeypatch, ["Books/Jane Doe - Orig (1999).pdf"], crud)
    rf.main()
    book, att = crud.created
    assert book["itemType"] == "book" and book["title"] == "OL Title"
    assert book["creators"] == [{"creatorType": "author", "firstName": "Real", "lastName": "Author"}]
    assert (book["date"], book["publisher"], book["ISBN"], book["abstractNote"]) == ("2005", "Pub", "isbn1", "")
    assert book["url"] == "dropbox://Books/Jane Doe - Orig (1999).pdf"
    assert book["tags"] == [{"tag": "dropbox-import-remediated"}]
    assert "collections" not in book
    assert att["parentItem"] == "BOOK" and att["itemType"] == "attachment"
    assert att["md5"] == hashlib.md5(b"BYTES").hexdigest()
    assert att["filename"] == "Jane Doe - Orig (1999).pdf"
    args = crud.uploaded[0]
    assert args[0] == "ATT" and args[3] == "application/pdf" and args[6] == 5
    assert recs(env)[-1]["status"] == "ok" and recs(env)[-1]["remediated"] is True
    assert staged(env) == []
    assert "Already ok: 0  Currently failing: 1" in capsys.readouterr().out


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="remediate_failed.py: every enriched item is logged metadata_source='google-books', "
                          "even when the record came from Open Library")
def test_main_metadata_source_names_open_library_when_it_supplied_the_record(env, monkeypatch):
    crud = StubCRUD()
    setup_main(env, monkeypatch, ["Books/Jane Doe - Orig (1999).pdf"], crud)   # Open Library hit
    rf.main()
    assert recs(env)[-1]["metadata_source"] != "google-books"


def test_main_uses_google_books_when_open_library_misses(env, monkeypatch):
    crud = StubCRUD()
    setup_main(env, monkeypatch, ["Books/Jane Doe - T (2001).pdf"], crud, enriched="gb")
    rf.main()
    book = crud.created[0]
    assert book["title"] == "GB Title" and book["abstractNote"] == "abs"
    # lookup gave no authors/date -> filename-derived ones are kept
    assert book["creators"][0]["lastName"] == "Doe" and book["date"] == "2001"


def test_main_filename_only_when_no_lookup_hits(env, monkeypatch):
    crud = StubCRUD()
    setup_main(env, monkeypatch, ["Books/Jane Doe - T (2001).pdf"], crud, enriched=None)
    rf.main()
    book = crud.created[0]
    assert (book["title"], book["date"], book["publisher"], book["ISBN"], book["abstractNote"]) == ("T", "2001", "", "", "")
    assert recs(env)[-1]["metadata_source"] == "filename-only"


def test_main_no_year_anywhere_gives_empty_date(env, monkeypatch):
    crud = StubCRUD()
    setup_main(env, monkeypatch, ["Books/Jane Doe - T.pdf"], crud, enriched=None)
    rf.main()
    assert crud.created[0]["date"] == ""


def test_main_collection_key_added(env, monkeypatch):
    monkeypatch.setattr(rf, "DROPBOX_IMPORT_COLLECTION_KEY", "COLL")
    crud = StubCRUD()
    setup_main(env, monkeypatch, ["Books/Jane Doe - T.pdf"], crud, enriched=None)
    rf.main()
    assert crud.created[0]["collections"] == ["COLL"]


def test_main_download_failure_logged_and_loop_continues(env, monkeypatch, capsys):
    crud = StubCRUD()
    setup_main(env, monkeypatch, ["a/x.pdf", "a/y.pdf"], crud, dl_ok=False)
    rf.main()
    assert crud.created == []
    stages = [(r["path"], r["stage"]) for r in recs(env) if r.get("stage") == "remediate-download"]
    assert stages == [("a/x.pdf", "remediate-download"), ("a/y.pdf", "remediate-download")]
    assert "[DL-FAIL] x.pdf: gone" in capsys.readouterr().out


def test_main_book_item_failure_removes_local_and_logs(env, monkeypatch, capsys):
    crud = StubCRUD(creates=[(500, {"failed": {"0": "no"}})])
    setup_main(env, monkeypatch, ["a/x.pdf"], crud)
    rf.main()
    assert staged(env) == []
    r = recs(env)[-1]
    assert (r["status"], r["stage"]) == ("fail", "remediate-item")
    assert "[ITEM-FAIL] x.pdf: 500" in capsys.readouterr().out


def test_main_book_item_without_key_is_failure_even_on_200(env, monkeypatch):
    crud = StubCRUD(creates=[(200, {"success": {}})])
    setup_main(env, monkeypatch, ["a/x.pdf"], crud)
    rf.main()
    assert recs(env)[-1]["stage"] == "remediate-item"


def test_main_attachment_item_failure(env, monkeypatch, capsys):
    crud = StubCRUD(creates=[(200, {"success": {"0": "BOOK"}}), (400, {"failed": {}})])
    setup_main(env, monkeypatch, ["a/x.pdf"], crud)
    rf.main()
    assert staged(env) == []
    assert recs(env)[-1]["stage"] == "remediate-attach-item"
    assert crud.uploaded == []
    assert "[ATTACH-ITEM-FAIL]" in capsys.readouterr().out


def test_main_upload_failure_logged(env, monkeypatch, capsys):
    crud = StubCRUD(upload=(False, 500, "x" * 500))
    setup_main(env, monkeypatch, ["a/x.pdf"], crud)
    rf.main()
    r = recs(env)[-1]
    assert (r["status"], r["stage"]) == ("fail", "remediate-upload") and len(r["error"]) == 300
    assert staged(env) == []
    assert "[UPLOAD-FAIL]" in capsys.readouterr().out


def test_main_only_processes_currently_failing_paths(env, monkeypatch):
    crud = StubCRUD()
    setup_main(env, monkeypatch, ["a/fixed.pdf"], crud, enriched=None)
    with open(env / "log.jsonl", "a") as f:
        f.write(json.dumps({"path": "a/fixed.pdf", "status": "ok"}) + "\n")
    rf.main()
    assert crud.created == []
