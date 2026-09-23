"""Library-relative storage of score paths (Phase A).

Setlists, the recent list, the hash index and the scan cache hold paths
relative to the library root on disk, so they survive the library being
mounted elsewhere; in memory and over the API paths stay absolute. Legacy
files holding absolute paths are converted once, with a backup, when the
library is opened.
"""

import json
import os
import re
import shutil

import pytest
from fastapi.testclient import TestClient

import web.core as core
import web.server as srv
from web.core import (
    MIGRATION_BACKUP_SUFFIX,
    SafeJSONError,
    from_library_relative,
    is_library_relative,
    load_setlists,
    migrate_to_relative,
    portable_path,
    save_setlists,
    to_library_relative,
    to_stored_path,
)
from web.server import app, state

STORES = ["setlists.json", "_recent.json", "_hash_index.json"]


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Isolate server config and state, as in test_web_api.py."""
    monkeypatch.setattr(srv, "WEB_CONFIG_PATH", str(tmp_path / "web_config.json"))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(srv, "CONFIG_DIR", str(config_dir))
    state.library_dir = ""
    state.scores = []
    state.config = {"last_directory": "", "allowed_roots": []}
    srv._rate_buckets.clear()
    yield
    state.library_dir = ""
    state.scores = []
    state.config = {"last_directory": "", "allowed_roots": []}


@pytest.fixture
def client():
    return TestClient(app)


def _read(path):
    with open(path) as f:
        return json.load(f)


def _write(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


@pytest.fixture
def legacy_lib(tmp_path):
    """A library whose reference files hold absolute paths (pre-Phase A)."""
    root = tmp_path / "Music"
    (root / "jazz").mkdir(parents=True)
    pdf = root / "jazz" / "Davis - Blue.pdf"
    pdf.write_bytes(b"%PDF-1.4 davis blue unique")
    p = portable_path(str(pdf))
    _write(root / "setlists.json", {"Gig": {"items": [
        {"type": "song", "path": p, "title": "Blue", "composer": "Davis",
         "start_page": 1, "end_page": None},
        {"type": "setlist_ref", "setlist_name": "Other"},
    ], "shuffle": False}})
    _write(root / "_recent.json", [{"filepath": p, "composer": "Davis",
                                    "title": "Blue", "timestamp": 1}])
    _write(root / "_hash_index.json", {"abc123": p})
    return root


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path, root, expected", [
    ("/lib/a/b.pdf", "/lib", "a/b.pdf"),
    ("/lib/a/b.pdf", "/lib/", "a/b.pdf"),
    ("/lib/./a//b.pdf", "/lib", "a/b.pdf"),
    ("a/b.pdf", "/lib", "a/b.pdf"),                 # already relative
    ("a\\b.pdf", "/lib", "a/b.pdf"),                # Windows separators
    ("./a/../b.pdf", "/lib", "b.pdf"),
    # Windows drive form and WSL mount form are the same place
    ("Z:/PARA/Music/a.pdf", "/mnt/z/PARA/Music", "a.pdf"),
    ("Z:\\PARA\\Music\\a.pdf", "/mnt/z/PARA/Music", "a.pdf"),
    ("z:/PARA/Music/a.pdf", "/mnt/z/PARA/Music", "a.pdf"),
    ("/mnt/z/PARA/Music/a.pdf", "Z:/PARA/Music", "a.pdf"),
    # Not inside the library
    ("/library2/x.pdf", "/lib", None),              # lookalike prefix
    ("/lib", "/lib", None),                         # the root itself
    ("/other/x.pdf", "/lib", None),
    ("/lib/../x.pdf", "/lib", None),
    ("../x.pdf", "/lib", None),
    ("..", "/lib", None),
    ("a/../../x.pdf", "/lib", None),
    ("", "/lib", None),
    ("x.pdf", "", None),                            # no library
])
def test_to_library_relative(path, root, expected):
    assert to_library_relative(path, root) == expected


@pytest.mark.parametrize("path, expected", [
    ("a/b.pdf", "/lib/a/b.pdf"),
    ("/lib/a/b.pdf", "/lib/a/b.pdf"),
    ("/other/x.pdf", "/other/x.pdf"),               # outside: unchanged
    ("../x.pdf", "../x.pdf"),                       # escapes root: unchanged
])
def test_from_library_relative(path, expected):
    assert from_library_relative(path, "/lib") == expected


def test_windows_form_is_normalised_to_the_root_form():
    assert (from_library_relative("Z:/PARA/Music/a.pdf", "/mnt/z/PARA/Music")
            == "/mnt/z/PARA/Music/a.pdf")


@pytest.mark.parametrize("path, root, expected", [
    ("/lib/a.pdf", "/lib", "a.pdf"),
    ("/other/a.pdf", "/lib", "/other/a.pdf"),
    ("/lib/a.pdf", "", "/lib/a.pdf"),               # no library: unchanged
])
def test_to_stored_path(path, root, expected):
    assert to_stored_path(path, root) == expected


def _windows_isabs(p: str) -> bool:
    """os.path.isabs as on Windows with Python 3.13+: a root-only path such
    as "/data/x.pdf" has no drive, so it is not absolute."""
    return bool(re.match(r"^[A-Za-z]:[/\\]", p))


def test_root_only_path_counts_as_absolute_under_windows_isabs(monkeypatch):
    """Regression: on Windows (Python 3.13+) a stored "/data/x.pdf" counted
    as relative, and would have been joined onto the library root."""
    monkeypatch.setattr(core.os.path, "isabs", _windows_isabs)
    assert not is_library_relative("/data/x.pdf")
    assert to_library_relative("/data/x.pdf", "D:/lib") is None
    assert from_library_relative("/data/x.pdf", "D:/lib") == "/data/x.pdf"
    assert is_library_relative("a/b.pdf")


def test_save_does_not_modify_callers_data(tmp_path):
    root = str(tmp_path)
    data = {"S": {"items": [{"type": "song", "path": portable_path(
        str(tmp_path / "a.pdf"))}], "shuffle": False}}
    before = json.loads(json.dumps(data))
    save_setlists(str(tmp_path / "setlists.json"), data, root)
    assert data == before
    assert _read(tmp_path / "setlists.json")["S"]["items"][0]["path"] == "a.pdf"
    assert load_setlists(str(tmp_path / "setlists.json"), root) == before


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_converts_backs_up_and_is_idempotent(legacy_lib):
    originals = {n: (legacy_lib / n).read_bytes() for n in STORES}
    report = migrate_to_relative(str(legacy_lib))
    assert report == {n: (1, []) for n in STORES}

    assert _read(legacy_lib / "setlists.json")["Gig"]["items"][0]["path"] \
        == "jazz/Davis - Blue.pdf"
    assert _read(legacy_lib / "setlists.json")["Gig"]["items"][1] \
        == {"type": "setlist_ref", "setlist_name": "Other"}
    assert _read(legacy_lib / "_recent.json")[0]["filepath"] == "jazz/Davis - Blue.pdf"
    assert _read(legacy_lib / "_hash_index.json") == {"abc123": "jazz/Davis - Blue.pdf"}
    for n in STORES:
        assert (legacy_lib / (n + MIGRATION_BACKUP_SUFFIX)).read_bytes() == originals[n]

    # A second run changes nothing -- not even the files' bytes.
    converted = {n: (legacy_lib / n).read_bytes() for n in STORES}
    assert migrate_to_relative(str(legacy_lib)) == {n: (0, []) for n in STORES}
    assert {n: (legacy_lib / n).read_bytes() for n in STORES} == converted


def test_migration_never_overwrites_an_existing_backup(legacy_lib):
    """A later partial migration must not replace the first, true original."""
    (legacy_lib / ("setlists.json" + MIGRATION_BACKUP_SUFFIX)).write_text("first")
    migrate_to_relative(str(legacy_lib))
    assert (legacy_lib / ("setlists.json" + MIGRATION_BACKUP_SUFFIX)).read_text() \
        == "first"


def test_migration_leaves_outside_paths_absolute_and_reports_them(legacy_lib):
    sl = _read(legacy_lib / "setlists.json")
    sl["Gig"]["items"].append({"type": "song", "path": "/elsewhere/x.pdf"})
    _write(legacy_lib / "setlists.json", sl)
    _write(legacy_lib / "_recent.json", [{"filepath": "/elsewhere/x.pdf"}])

    report = migrate_to_relative(str(legacy_lib))
    assert report["setlists.json"] == (1, ["/elsewhere/x.pdf"])
    assert report["_recent.json"] == (0, ["/elsewhere/x.pdf"])
    items = _read(legacy_lib / "setlists.json")["Gig"]["items"]
    assert items[0]["path"] == "jazz/Davis - Blue.pdf"
    assert items[2]["path"] == "/elsewhere/x.pdf"
    # Nothing to convert in the recent list: not rewritten, not backed up.
    assert _read(legacy_lib / "_recent.json") == [{"filepath": "/elsewhere/x.pdf"}]
    assert not (legacy_lib / ("_recent.json" + MIGRATION_BACKUP_SUFFIX)).exists()


def test_migration_skips_malformed_entries(legacy_lib):
    sl = _read(legacy_lib / "setlists.json")
    sl["Gig"]["items"] += ["junk", None, {"type": "song", "path": 3}]
    _write(legacy_lib / "setlists.json", sl)
    _write(legacy_lib / "_hash_index.json", {"abc123": portable_path(
        str(legacy_lib / "jazz" / "Davis - Blue.pdf")), "bad": ["x"]})
    migrate_to_relative(str(legacy_lib))
    items = _read(legacy_lib / "setlists.json")["Gig"]["items"]
    assert items[2:] == ["junk", None, {"type": "song", "path": 3}]
    assert _read(legacy_lib / "_hash_index.json") == {
        "abc123": "jazz/Davis - Blue.pdf", "bad": ["x"]}


def test_migration_missing_files_are_fine(tmp_path):
    lib = tmp_path / "empty"
    lib.mkdir()
    assert migrate_to_relative(str(lib)) == {}
    assert os.listdir(lib) == []


def test_migration_raises_on_corrupt_file(legacy_lib):
    (legacy_lib / "_recent.json").write_text("{not json")
    with pytest.raises(SafeJSONError):
        migrate_to_relative(str(legacy_lib))


def test_backup_failure_raises_safejsonerror_and_leaves_file(legacy_lib, monkeypatch):
    """Regression: an OSError from the backup copy escaped as-is."""
    def no_copy(src, dst):
        raise PermissionError("read-only library")

    monkeypatch.setattr(shutil, "copy2", no_copy)
    before = (legacy_lib / "setlists.json").read_bytes()
    with pytest.raises(SafeJSONError, match="Cannot back up"):
        migrate_to_relative(str(legacy_lib))
    assert (legacy_lib / "setlists.json").read_bytes() == before


# ---------------------------------------------------------------------------
# Server: converts on open, absolute at the API, relative on disk
# ---------------------------------------------------------------------------


def test_opening_a_legacy_library_converts_it_and_api_stays_absolute(
        client, legacy_lib):
    pdf_path = portable_path(str(legacy_lib / "jazz" / "Davis - Blue.pdf"))
    assert client.post("/api/library", json={"path": str(legacy_lib)}).status_code == 200

    assert _read(legacy_lib / "setlists.json")["Gig"]["items"][0]["path"] \
        == "jazz/Davis - Blue.pdf"
    assert (legacy_lib / ("setlists.json" + MIGRATION_BACKUP_SUFFIX)).exists()
    assert client.get("/api/setlists/Gig").json()["items"][0]["path"] == pdf_path
    assert client.get("/api/recent").json()["recent"][0]["filepath"] == pdf_path
    flat = client.get("/api/setlists/Gig/flat").json()
    assert pdf_path in json.dumps(flat)


def test_read_only_library_still_opens(client, legacy_lib, monkeypatch):
    """Regression: a failed migration backup (e.g. a read-only mount) made
    the library fail to open at all."""
    def no_copy(src, dst):
        raise PermissionError("read-only library")

    monkeypatch.setattr(shutil, "copy2", no_copy)
    before = (legacy_lib / "setlists.json").read_bytes()
    state.set_library(str(legacy_lib))

    assert len(state.scores) == 1
    assert (legacy_lib / "setlists.json").read_bytes() == before
    pdf_path = portable_path(str(legacy_lib / "jazz" / "Davis - Blue.pdf"))
    assert client.get("/api/setlists/Gig").json()["items"][0]["path"] == pdf_path


def test_corrupt_reference_file_does_not_stop_the_library_opening(legacy_lib):
    (legacy_lib / "_recent.json").write_text("{not json")
    state.set_library(str(legacy_lib))
    assert len(state.scores) == 1


def test_api_writes_are_stored_relative(client, legacy_lib):
    state.set_library(str(legacy_lib))
    pdf_path = portable_path(str(legacy_lib / "jazz" / "Davis - Blue.pdf"))

    client.post("/api/setlists", json={"name": "New"})
    resp = client.put("/api/setlists/New", json={"items": [
        {"type": "song", "path": pdf_path, "title": "Blue", "composer": "Davis"},
    ]})
    assert resp.status_code == 200
    assert _read(legacy_lib / "setlists.json")["New"]["items"][0]["path"] \
        == "jazz/Davis - Blue.pdf"
    assert client.get("/api/setlists/New").json()["items"][0]["path"] == pdf_path

    assert client.post("/api/recent", json={"path": pdf_path}).status_code == 200
    assert _read(legacy_lib / "_recent.json")[0]["filepath"] == "jazz/Davis - Blue.pdf"

    cache = _read(legacy_lib / "_scan_cache.json")
    assert list(cache) == ["jazz/Davis - Blue.pdf"]
    assert _read(legacy_lib / "_hash_index.json") == {
        state.scores[0].content_hash: "jazz/Davis - Blue.pdf"}


def test_library_mounted_elsewhere_keeps_its_references(client, legacy_lib, tmp_path):
    """The point of Phase A: after conversion, moving (re-mounting) the
    library changes no stored data, and the API reports the new location."""
    state.set_library(str(legacy_lib))
    moved = tmp_path / "mounted" / "Music"
    moved.parent.mkdir()
    os.rename(legacy_lib, moved)
    before = {n: (moved / n).read_bytes() for n in STORES}

    assert client.post("/api/library", json={"path": str(moved)}).status_code == 200
    pdf_path = portable_path(str(moved / "jazz" / "Davis - Blue.pdf"))
    assert client.get("/api/setlists/Gig").json()["items"][0]["path"] == pdf_path
    assert client.get("/api/recent").json()["recent"][0]["filepath"] == pdf_path
    # Not mistaken for a rename: nothing was healed or rewritten.
    assert {n: (moved / n).read_bytes() for n in STORES} == before


def test_remount_keeps_entries_the_hash_heal_cannot_fix(client, tmp_path):
    """Regression: before Phase A a remount was healed only by content hash,
    which skips duplicate-content files -- their setlist entries kept
    pointing at the old mount path, so the song no longer opened."""
    lib = tmp_path / "container" / "Music"
    (lib / "copies").mkdir(parents=True)
    for d in (lib, lib / "copies"):
        (d / "Bach - Suite.pdf").write_bytes(b"%PDF-1.4 same bytes")
    state.set_library(str(lib))
    client.post("/api/setlists", json={"name": "Gig"})
    client.put("/api/setlists/Gig", json={"items": [{
        "type": "song", "path": portable_path(str(lib / "Bach - Suite.pdf")),
        "title": "Suite", "composer": "Bach"}]})

    moved = tmp_path / "host" / "Music"
    moved.parent.mkdir()
    os.rename(lib, moved)
    state.set_library(str(moved))
    assert client.get("/api/setlists/Gig").json()["items"][0]["path"] \
        == portable_path(str(moved / "Bach - Suite.pdf"))
