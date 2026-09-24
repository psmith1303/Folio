"""The API gives and takes score paths relative to the library root (Step 10b).

Clients key everything by these paths: the page's state, and each device's
offline copies (Cache Storage keys and LRU entries). Absolute paths made
those depend on where the library is mounted, so moving the container's
mount (to /library) would have orphaned every device's offline copies.
Relative input still goes through the traversal check.
"""

import os

import pytest
from fastapi.testclient import TestClient

import web.server as srv
from web.core import portable_path
from web.server import app, state

DAVIS = "jazz/Davis - Blue.pdf"
BACH = "Bach - Suite.pdf"


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


@pytest.fixture
def outside(tmp_path):
    """A real PDF beside the library, so a traversal that escaped would find
    it (200), not miss it (404)."""
    p = tmp_path / "secret.pdf"
    p.write_bytes(b"%PDF-1.4 secret")
    return p


@pytest.fixture
def lib(tmp_path, client, outside):
    """A library with two scores, a setlist holding both plus a song outside
    the library and a reference to another setlist, and a recent entry.
    Set up with absolute paths, which the old API took too, so each test
    below certifies against it on its own."""
    root = tmp_path / "Music"
    a = lambda rel: portable_path(str(root / rel))
    (root / "jazz").mkdir(parents=True)
    (root / DAVIS).write_bytes(b"%PDF-1.4 davis")
    (root / BACH).write_bytes(b"%PDF-1.4 bach")
    assert client.post("/api/library", json={"path": str(root)}).status_code == 200
    for name in ("Gig", "Encore"):
        client.post("/api/setlists", json={"name": name})
    assert client.put("/api/setlists/Encore", json={"items": [
        {"type": "song", "path": a(BACH), "title": "Suite", "composer": "Bach"},
    ]}).status_code == 200
    assert client.put("/api/setlists/Gig", json={"items": [
        {"type": "song", "path": a(DAVIS), "title": "Blue", "composer": "Davis"},
        {"type": "song", "path": portable_path(str(outside)), "title": "Secret"},
        {"type": "setlist_ref", "setlist_name": "Encore"},
    ]}).status_code == 200
    assert client.post("/api/recent", json={"path": a(DAVIS)}).status_code == 200
    return root


def _song_paths(items):
    return [i["path"] for i in items if i.get("type", "song") == "song"]


# Each endpoint that returns score paths -> the paths in its response.
RESPONSES = {
    "library": lambda c: [s["filepath"] for s in c.get("/api/library").json()["scores"]],
    "newest": lambda c: [s["filepath"] for s in c.get("/api/newest").json()["scores"]],
    "score": lambda c: [c.get("/api/scores", params={"path": DAVIS}).json()["filepath"]],
    "recent": lambda c: [e["filepath"] for e in c.get("/api/recent").json()["recent"]],
    "setlist": lambda c: _song_paths(c.get("/api/setlists/Gig").json()["items"]),
    "flat": lambda c: _song_paths(c.get("/api/setlists/Gig/flat").json()["songs"]),
    "playback": lambda c: _song_paths(c.get("/api/setlists/Gig/playback").json()["songs"]),
}


def _in_library(paths, outside):
    """*paths* without the setlist's song from outside the library."""
    return sorted(p for p in paths if p != portable_path(str(outside)))


@pytest.mark.parametrize("endpoint", RESPONSES)
def test_every_path_the_api_returns_is_relative(client, lib, outside, endpoint):
    paths = RESPONSES[endpoint](client)
    assert paths, "the response carried no paths"
    assert set(_in_library(paths, outside)) <= {DAVIS, BACH}
    assert not any(str(lib) in p for p in paths)


def test_a_path_outside_the_library_is_returned_unchanged(client, lib, outside):
    """It has no relative form; it stays absolute rather than being mangled."""
    assert portable_path(str(outside)) in RESPONSES["setlist"](client)


def test_the_api_paths_do_not_change_when_the_library_moves(client, lib, outside, tmp_path):
    """The point of Step 10b: remounting the library (e.g. at /library) keeps
    every path a device has cached a PDF under."""
    before = {e: sorted(RESPONSES[e](client)) for e in RESPONSES if e != "playback"}
    moved = tmp_path / "library"
    os.rename(lib, moved)
    assert client.post("/api/library", json={"path": str(moved)}).status_code == 200
    after = {e: sorted(RESPONSES[e](client)) for e in RESPONSES if e != "playback"}
    assert after == before


def test_the_tag_edit_response_gives_the_renamed_path_relative(client, lib):
    resp = client.put("/api/scores/tags", json={"path": DAVIS, "filename_tags": ["cool"]})
    assert resp.status_code == 200
    new = resp.json()["score"]["filepath"]
    assert new == "jazz/Davis - Blue -- cool.pdf"
    # ...and the setlist and recent list now hold the same relative path.
    assert new in RESPONSES["setlist"](client)
    assert RESPONSES["recent"](client) == [new]


# ---------------------------------------------------------------------------
# Relative input
# ---------------------------------------------------------------------------


def test_relative_paths_are_accepted_everywhere_a_path_is_taken(client, lib):
    pdf = client.get("/api/pdf", params={"path": DAVIS})
    assert pdf.status_code == 200 and pdf.content == b"%PDF-1.4 davis"
    assert client.get("/api/scores", params={"path": DAVIS}).status_code == 200
    assert client.get("/api/annotations", params={"path": DAVIS}).status_code == 200
    put = client.put("/api/annotations", json={
        "path": DAVIS, "pages": {"0": [{"type": "ink"}]}, "rotations": {}})
    assert put.status_code == 200
    assert (lib / "jazz" / "Davis - Blue.json").exists()
    assert client.post("/api/recent", json={"path": BACH}).status_code == 200
    assert RESPONSES["recent"](client)[0] == BACH


def test_absolute_paths_inside_the_library_are_still_accepted(client, lib):
    abs_path = portable_path(str(lib / DAVIS))
    assert client.get("/api/pdf", params={"path": abs_path}).status_code == 200


def test_a_relative_path_that_stays_inside_resolves(client, lib):
    """Control for the traversal cases below: ".." that stays inside works."""
    assert client.get("/api/pdf", params={"path": f"jazz/../{BACH}"}).status_code == 200


@pytest.mark.parametrize("path", [
    "../secret.pdf",
    "jazz/../../secret.pdf",
    "./../secret.pdf",
    "..\\secret.pdf",
    "jazz\\..\\..\\secret.pdf",
])
@pytest.mark.parametrize("endpoint", ["/api/pdf", "/api/annotations", "/api/scores"])
def test_relative_traversal_out_of_the_library_is_refused(client, lib, outside, endpoint, path):
    assert client.get(endpoint, params={"path": path}).status_code == 403


def test_relative_traversal_cannot_write_annotations_outside(client, lib, outside):
    resp = client.put("/api/annotations", json={
        "path": "../secret.pdf", "pages": {}, "rotations": {}})
    assert resp.status_code == 403
    assert not (outside.parent / "secret.json").exists()
