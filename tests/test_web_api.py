"""Tests for web.server — FastAPI endpoints."""

import os

import pytest
from fastapi.testclient import TestClient

import web.server as srv
from web.core import SafeJSON, load_hash_index, load_recent, load_setlists
from web.server import app, state


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Reset server state and isolate config writes to a temp file."""
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
def library_with_pdfs(tmp_path):
    """Create a temp directory with fake PDFs and set it as library."""
    # Create minimal valid PDF files (pdf.js won't parse these, but
    # the API only needs them to exist for serving and page-count tests)
    for name in ["Bach - Cello Suite.pdf", "Mozart - Sonata.pdf"]:
        (tmp_path / name).write_bytes(b"%PDF-1.4 fake")
    sub = tmp_path / "jazz"
    sub.mkdir()
    (sub / "Davis - Blue -- swing.pdf").write_bytes(b"%PDF-1.4 fake")
    return str(tmp_path)


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------


class TestGetConfig:
    def test_returns_config(self, client):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "library_dir" in data
        assert "score_count" in data

    def test_returns_default_keybindings(self, client):
        resp = client.get("/api/config")
        kb = resp.json()["keybindings"]
        assert kb["go_library"] == "Alt+l"
        assert kb["go_setlists"] == "Alt+s"
        assert kb["go_recent"] == "Alt+r"
        assert kb["focus_search"] == "Ctrl+f"
        assert kb["tool_nav"] == "v"

    def test_user_keybinding_overrides(self, client):
        state.config["keybindings"] = {"go_library": "Alt+1"}
        resp = client.get("/api/config")
        kb = resp.json()["keybindings"]
        assert kb["go_library"] == "Alt+1"
        # Other defaults still present
        assert kb["go_setlists"] == "Alt+s"


# ---------------------------------------------------------------------------
# Obsolete config keys (left behind by the removed auth mechanism)
# ---------------------------------------------------------------------------


class TestObsoleteConfigKeys:
    @pytest.mark.parametrize("stale", [
        {"auth_salt": "xyzzy"},
        {"session_secret": "deadbeef"},
        {"auth_salt": "xyzzy", "session_secret": "deadbeef"},
    ])
    def test_stale_keys_dropped_from_result_and_disk(self, stale):
        srv._save_config({"last_directory": "/music", **stale})
        cfg = srv._load_config()
        on_disk = SafeJSON.load(srv.WEB_CONFIG_PATH)
        for key in stale:
            assert key not in cfg
            assert key not in on_disk

    def test_surviving_keys_preserved(self):
        srv._save_config({
            "last_directory": "/music",
            "allowed_roots": ["/music"],
            "keybindings": {"undo": "Ctrl+z"},
            "auth_salt": "xyzzy",
            "session_secret": "deadbeef",
        })
        cfg = srv._load_config()
        assert cfg["last_directory"] == "/music"
        assert cfg["allowed_roots"] == ["/music"]
        assert cfg["keybindings"] == {"undo": "Ctrl+z"}

    def test_clean_config_is_not_rewritten(self, monkeypatch):
        """A config with no stale keys must not be saved on every load."""
        srv._save_config({"last_directory": "/music"})
        saves: list[dict] = []
        monkeypatch.setattr(srv, "_save_config", saves.append)
        srv._load_config()
        assert saves == []

    def test_strip_is_idempotent(self, monkeypatch):
        """Second load finds nothing stale and does not rewrite the file."""
        srv._save_config({"last_directory": "/music", "auth_salt": "xyzzy"})
        srv._load_config()
        saves: list[dict] = []
        monkeypatch.setattr(srv, "_save_config", saves.append)
        cfg = srv._load_config()
        assert saves == []
        assert "auth_salt" not in cfg

    def test_corrupt_config_is_not_rewritten(self, monkeypatch):
        """A corrupt file falls back to defaults without clobbering itself."""
        with open(srv.WEB_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("{ not json")
        saves: list[dict] = []
        monkeypatch.setattr(srv, "_save_config", saves.append)
        cfg = srv._load_config()
        assert saves == []
        assert cfg == srv.DEFAULT_WEB_CONFIG


# ---------------------------------------------------------------------------
# POST /api/library
# ---------------------------------------------------------------------------


class TestSetLibrary:
    def test_set_valid_directory(self, client, library_with_pdfs):
        resp = client.post("/api/library",
                           json={"path": library_with_pdfs})
        assert resp.status_code == 200
        data = resp.json()
        assert data["score_count"] == 3

    def test_set_nonexistent_directory(self, client):
        resp = client.post("/api/library",
                           json={"path": "/nonexistent/dir"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/library
# ---------------------------------------------------------------------------


class TestGetLibrary:
    def test_empty_library(self, client):
        resp = client.get("/api/library")
        assert resp.status_code == 200
        assert resp.json()["scores"] == []

    def test_lists_scores(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get("/api/library")
        data = resp.json()
        assert data["total"] == 3

    @pytest.mark.parametrize("query", [
        "q=bach", "composer=Mozart", "tag=jazz", "sort=title&desc=true",
    ])
    def test_query_parameters_are_ignored(self, client, library_with_pdfs, query):
        """/api/library is a plain list: clients filter and sort it themselves
        (library-filter.js), so every query string gets the same full list."""
        state.set_library(library_with_pdfs)
        bare = client.get("/api/library").json()
        assert client.get(f"/api/library?{query}").json() == bare
        assert sorted(s["composer"] for s in bare["scores"]) == ["Bach", "Davis", "Mozart"]

    def test_scores_carry_what_clients_filter_on(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        data = client.get("/api/library").json()
        assert set(data) == {"scores", "total"}
        for sc in data["scores"]:
            assert {"filepath", "composer", "title", "tags"} <= set(sc)
        davis = next(sc for sc in data["scores"] if sc["composer"] == "Davis")
        assert davis["tags"] == ["jazz", "swing"]


# ---------------------------------------------------------------------------
# GET /api/pdf
# ---------------------------------------------------------------------------


class TestServePDF:
    def test_serves_pdf(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        # Get a filepath from the library
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]
        resp = client.get(f"/api/pdf?path={path}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"

    def test_no_library_returns_400(self, client):
        resp = client.get("/api/pdf?path=/some/file.pdf")
        assert resp.status_code == 400

    def test_path_traversal_blocked(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get(f"/api/pdf?path={library_with_pdfs}/../../../etc/passwd")
        assert resp.status_code in (403, 404)

    def test_nonexistent_file_returns_404(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get(f"/api/pdf?path={library_with_pdfs}/nope.pdf")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/annotations
# ---------------------------------------------------------------------------


class TestGetAnnotations:
    def test_returns_empty_for_unannotated_pdf(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]
        resp = client.get(f"/api/annotations?path={path}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == 2
        assert data["pages"] == {}

    def test_no_library_returns_400(self, client):
        resp = client.get("/api/annotations?path=/some/file.pdf")
        assert resp.status_code == 400

    def test_nonexistent_pdf_returns_404(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get(f"/api/annotations?path={library_with_pdfs}/nope.pdf")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/annotations
# ---------------------------------------------------------------------------


class TestPutAnnotations:
    def test_save_and_reload(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]

        pages = {"0": [{"uuid": "test-123", "type": "ink",
                        "points": [[0.1, 0.2], [0.3, 0.4]],
                        "color": "red", "width": 3}]}
        resp = client.put("/api/annotations", json={
            "path": path, "pages": pages, "rotations": {}
        })
        assert resp.status_code == 200

        # Reload and verify
        resp = client.get(f"/api/annotations?path={path}")
        data = resp.json()
        assert len(data["pages"]["0"]) == 1
        assert data["pages"]["0"][0]["color"] == "red"

    def test_startpage_round_trip(self, client, library_with_pdfs):
        """The start-page stamp is stored as an ordinary annotation."""
        state.set_library(library_with_pdfs)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]

        stamp = {"uuid": "s1", "type": "startpage", "x": 0.8, "y": 0.1}
        resp = client.put("/api/annotations", json={
            "path": path, "pages": {"2": [stamp]}, "rotations": {}
        })
        assert resp.status_code == 200

        resp = client.get(f"/api/annotations?path={path}")
        assert resp.json()["pages"]["2"] == [stamp]

    def test_rotation_round_trip(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]

        resp = client.put("/api/annotations", json={
            "path": path, "pages": {},
            "rotations": {"0": 90, "1": 270}
        })
        assert resp.status_code == 200

        resp = client.get(f"/api/annotations?path={path}")
        data = resp.json()
        assert data["rotations"]["0"] == 90
        assert data["rotations"]["1"] == 270

    def test_zero_rotation_not_persisted(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]

        resp = client.put("/api/annotations", json={
            "path": path, "pages": {},
            "rotations": {"0": 360, "1": 90}
        })
        assert resp.status_code == 200

        resp = client.get(f"/api/annotations?path={path}")
        data = resp.json()
        assert "0" not in data["rotations"]
        assert data["rotations"]["1"] == 90

    def test_save_returns_etag(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]

        resp = client.put("/api/annotations", json={
            "path": path, "pages": {}, "rotations": {}
        })
        assert resp.status_code == 200
        assert "etag" in resp.json()
        assert len(resp.json()["etag"]) > 0

    def test_get_returns_etag(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]

        # Save to create the sidecar
        client.put("/api/annotations", json={
            "path": path, "pages": {}, "rotations": {}
        })
        resp = client.get(f"/api/annotations?path={path}")
        assert "etag" in resp.json()

    def test_save_with_correct_etag_succeeds(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]

        # Initial save
        resp = client.put("/api/annotations", json={
            "path": path, "pages": {}, "rotations": {}
        })
        etag = resp.json()["etag"]

        # Save with the correct etag
        resp = client.put("/api/annotations", json={
            "path": path, "pages": {"0": []}, "rotations": {},
            "expected_etag": etag,
        })
        assert resp.status_code == 200

    def test_save_with_stale_etag_returns_409(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]

        # Initial save — get etag
        resp = client.put("/api/annotations", json={
            "path": path, "pages": {}, "rotations": {}
        })
        stale_etag = resp.json()["etag"]

        # Concurrent edit (changes the file)
        client.put("/api/annotations", json={
            "path": path,
            "pages": {"0": [{"uuid": "other", "type": "ink",
                             "points": [[0.1, 0.2]], "color": "red",
                             "width": 1}]},
            "rotations": {}
        })

        # Try to save with the stale etag
        resp = client.put("/api/annotations", json={
            "path": path, "pages": {}, "rotations": {},
            "expected_etag": stale_etag,
        })
        assert resp.status_code == 409

    def test_save_without_etag_always_succeeds(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]

        # Save without expected_etag — backwards compatible, no conflict check
        client.put("/api/annotations", json={
            "path": path, "pages": {}, "rotations": {}
        })
        resp = client.put("/api/annotations", json={
            "path": path, "pages": {"0": []}, "rotations": {}
        })
        assert resp.status_code == 200

    def test_path_traversal_blocked(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.put("/api/annotations", json={
            "path": f"{library_with_pdfs}/../../../etc/passwd",
            "pages": {}, "rotations": {}
        })
        assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# Setlist CRUD
# ---------------------------------------------------------------------------


class TestSetlists:
    def test_list_empty(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get("/api/setlists")
        assert resp.status_code == 200
        assert resp.json()["setlists"] == []

    def test_create(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.post("/api/setlists", json={"name": "My Set"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "My Set"

        resp = client.get("/api/setlists")
        assert len(resp.json()["setlists"]) == 1
        assert resp.json()["setlists"][0]["name"] == "My Set"

    def test_create_duplicate_returns_409(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Dup"})
        resp = client.post("/api/setlists", json={"name": "Dup"})
        assert resp.status_code == 409

    def test_create_empty_name_returns_400(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.post("/api/setlists", json={"name": "  "})
        assert resp.status_code == 400

    def test_get_setlist(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Test"})
        resp = client.get("/api/setlists/Test")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Test"
        assert resp.json()["items"] == []

    def test_get_nonexistent_returns_404(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get("/api/setlists/Nope")
        assert resp.status_code == 404

    def test_update_songs(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Gig"})
        songs = [{"path": "a.pdf", "title": "A", "composer": "X",
                  "start_page": 1, "end_page": None}]
        resp = client.put("/api/setlists/Gig", json={"songs": songs})
        assert resp.status_code == 200

        resp = client.get("/api/setlists/Gig")
        assert len(resp.json()["items"]) == 1
        assert resp.json()["items"][0]["title"] == "A"
        assert resp.json()["items"][0]["type"] == "song"

    def test_update_nonexistent_returns_404(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.put("/api/setlists/Nope", json={"songs": []})
        assert resp.status_code == 404

    def test_delete(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Gone"})
        resp = client.delete("/api/setlists/Gone")
        assert resp.status_code == 200

        resp = client.get("/api/setlists")
        assert resp.json()["setlists"] == []

    def test_delete_nonexistent_returns_404(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.delete("/api/setlists/Nope")
        assert resp.status_code == 404

    def test_rename(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Old"})
        resp = client.post("/api/setlists/Old/rename",
                           json={"new_name": "New"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"

        resp = client.get("/api/setlists")
        names = [s["name"] for s in resp.json()["setlists"]]
        assert "New" in names
        assert "Old" not in names

    def test_rename_to_existing_returns_409(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "A"})
        client.post("/api/setlists", json={"name": "B"})
        resp = client.post("/api/setlists/A/rename",
                           json={"new_name": "B"})
        assert resp.status_code == 409

    def test_rename_nonexistent_returns_404(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.post("/api/setlists/Nope/rename",
                           json={"new_name": "X"})
        assert resp.status_code == 404

    def test_create_invalid_name_returns_400(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.post("/api/setlists", json={"name": "bad/name"})
        assert resp.status_code == 400

    def test_create_too_long_name_returns_400(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.post("/api/setlists", json={"name": "x" * 201})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Nested setlists
# ---------------------------------------------------------------------------


def _song(title: str = "S", composer: str = "C") -> dict:
    return {"type": "song", "path": f"{title}.pdf", "title": title,
            "composer": composer, "start_page": 1, "end_page": None}


def _ref(name: str) -> dict:
    return {"type": "setlist_ref", "setlist_name": name}


class TestNestedSetlists:
    def test_create_with_setlist_ref(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Warm-up"})
        client.put("/api/setlists/Warm-up",
                   json={"items": [_song("Scales")]})
        client.post("/api/setlists", json={"name": "Monday"})
        resp = client.put("/api/setlists/Monday",
                          json={"items": [_ref("Warm-up"), _song("Etude")]})
        assert resp.status_code == 200

        resp = client.get("/api/setlists/Monday")
        items = resp.json()["items"]
        assert len(items) == 2
        assert items[0]["type"] == "setlist_ref"
        assert items[0]["setlist_name"] == "Warm-up"
        assert items[1]["type"] == "song"

    def test_get_setlist_enriches_refs(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Sub"})
        client.put("/api/setlists/Sub",
                   json={"items": [_song("A"), _song("B")]})
        client.post("/api/setlists", json={"name": "Main"})
        client.put("/api/setlists/Main", json={"items": [_ref("Sub")]})

        resp = client.get("/api/setlists/Main")
        ref_item = resp.json()["items"][0]
        assert ref_item["exists"] is True
        assert ref_item["flat_count"] == 2

    def test_get_setlist_enriches_dangling_ref(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Main"})
        client.put("/api/setlists/Main",
                   json={"items": [_ref("Ghost")]})

        resp = client.get("/api/setlists/Main")
        ref_item = resp.json()["items"][0]
        assert ref_item["exists"] is False
        assert ref_item["flat_count"] == 0

    def test_flat_simple(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Solo"})
        client.put("/api/setlists/Solo",
                   json={"items": [_song("A"), _song("B")]})

        resp = client.get("/api/setlists/Solo/flat")
        assert resp.status_code == 200
        songs = resp.json()["songs"]
        assert len(songs) == 2
        assert songs[0]["title"] == "A"

    def test_flat_nested(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Warm-up"})
        client.put("/api/setlists/Warm-up",
                   json={"items": [_song("Scales"), _song("LongTones")]})
        client.post("/api/setlists", json={"name": "Monday"})
        client.put("/api/setlists/Monday",
                   json={"items": [_ref("Warm-up"), _song("Etude")]})

        resp = client.get("/api/setlists/Monday/flat")
        songs = resp.json()["songs"]
        assert len(songs) == 3
        assert [s["title"] for s in songs] == ["Scales", "LongTones", "Etude"]

    def test_flat_deeply_nested(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "C"})
        client.put("/api/setlists/C", json={"items": [_song("Deep")]})
        client.post("/api/setlists", json={"name": "B"})
        client.put("/api/setlists/B", json={"items": [_ref("C")]})
        client.post("/api/setlists", json={"name": "A"})
        client.put("/api/setlists/A", json={"items": [_ref("B")]})

        resp = client.get("/api/setlists/A/flat")
        songs = resp.json()["songs"]
        assert len(songs) == 1
        assert songs[0]["title"] == "Deep"

    def test_flat_dangling_ref_skipped(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Main"})
        client.put("/api/setlists/Main",
                   json={"items": [_song("A"), _ref("Gone"), _song("B")]})

        resp = client.get("/api/setlists/Main/flat")
        songs = resp.json()["songs"]
        assert len(songs) == 2
        assert [s["title"] for s in songs] == ["A", "B"]

    def test_flat_diamond_includes_both(self, client, library_with_pdfs):
        """Diamond: A->B->D, A->C->D. D's songs appear twice."""
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "D"})
        client.put("/api/setlists/D", json={"items": [_song("Shared")]})
        client.post("/api/setlists", json={"name": "B"})
        client.put("/api/setlists/B", json={"items": [_ref("D")]})
        client.post("/api/setlists", json={"name": "C"})
        client.put("/api/setlists/C", json={"items": [_ref("D")]})
        client.post("/api/setlists", json={"name": "A"})
        client.put("/api/setlists/A", json={"items": [_ref("B"), _ref("C")]})

        resp = client.get("/api/setlists/A/flat")
        songs = resp.json()["songs"]
        assert len(songs) == 2
        assert all(s["title"] == "Shared" for s in songs)

    def test_circular_reference_rejected(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "A"})
        client.post("/api/setlists", json={"name": "B"})
        client.put("/api/setlists/A", json={"items": [_ref("B")]})
        resp = client.put("/api/setlists/B", json={"items": [_ref("A")]})
        assert resp.status_code == 400
        assert "Circular" in resp.json()["detail"]

    def test_self_reference_rejected(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Loop"})
        resp = client.put("/api/setlists/Loop",
                          json={"items": [_ref("Loop")]})
        assert resp.status_code == 400
        assert "Circular" in resp.json()["detail"]

    def test_rename_cascades_to_refs(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Sub"})
        client.put("/api/setlists/Sub", json={"items": [_song("X")]})
        client.post("/api/setlists", json={"name": "Parent"})
        client.put("/api/setlists/Parent", json={"items": [_ref("Sub")]})

        resp = client.post("/api/setlists/Sub/rename",
                           json={"new_name": "NewSub"})
        assert resp.status_code == 200

        resp = client.get("/api/setlists/Parent")
        ref = resp.json()["items"][0]
        assert ref["setlist_name"] == "NewSub"

    def test_delete_leaves_dangling_refs(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Sub"})
        client.put("/api/setlists/Sub", json={"items": [_song("X")]})
        client.post("/api/setlists", json={"name": "Parent"})
        client.put("/api/setlists/Parent",
                   json={"items": [_ref("Sub"), _song("Y")]})

        client.delete("/api/setlists/Sub")
        resp = client.get("/api/setlists/Parent/flat")
        songs = resp.json()["songs"]
        assert len(songs) == 1
        assert songs[0]["title"] == "Y"

    def test_backward_compat_items_without_type(self, client, library_with_pdfs):
        """Items without a type field are treated as songs."""
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Legacy"})
        legacy = [{"path": "a.pdf", "title": "A", "composer": "X",
                   "start_page": 1, "end_page": None}]
        client.put("/api/setlists/Legacy", json={"songs": legacy})

        resp = client.get("/api/setlists/Legacy")
        assert resp.json()["items"][0]["type"] == "song"

    def test_list_includes_flat_count(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Sub"})
        client.put("/api/setlists/Sub",
                   json={"items": [_song("A"), _song("B")]})
        client.post("/api/setlists", json={"name": "Parent"})
        client.put("/api/setlists/Parent",
                   json={"items": [_ref("Sub"), _song("C")]})

        resp = client.get("/api/setlists")
        by_name = {s["name"]: s for s in resp.json()["setlists"]}
        assert by_name["Parent"]["count"] == 2
        assert by_name["Parent"]["flat_count"] == 3
        assert by_name["Sub"]["count"] == 2
        assert by_name["Sub"]["flat_count"] == 2

    def test_mixed_types_roundtrip(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Sub"})
        client.post("/api/setlists", json={"name": "Mix"})
        items = [_song("First"), _ref("Sub"), _song("Last")]
        resp = client.put("/api/setlists/Mix", json={"items": items})
        assert resp.status_code == 200

        resp = client.get("/api/setlists/Mix")
        result = resp.json()["items"]
        assert len(result) == 3
        assert result[0]["type"] == "song"
        assert result[1]["type"] == "setlist_ref"
        assert result[2]["type"] == "song"

    def test_setlist_ref_empty_name_returns_400(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Bad"})
        resp = client.put("/api/setlists/Bad",
                          json={"items": [{"type": "setlist_ref",
                                           "setlist_name": ""}]})
        assert resp.status_code == 400

    def test_unknown_item_type_returns_400(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Bad"})
        resp = client.put("/api/setlists/Bad",
                          json={"items": [{"type": "widget"}]})
        assert resp.status_code == 400

    def test_flat_nonexistent_returns_404(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get("/api/setlists/Nope/flat")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------


class TestSecurity:
    def test_set_library_outside_allowed_roots(self, client, tmp_path):
        """When allowed_roots is set, directories outside are rejected."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        forbidden = tmp_path / "forbidden"
        forbidden.mkdir()
        state.config["allowed_roots"] = [str(allowed)]
        resp = client.post("/api/library", json={"path": str(forbidden)})
        assert resp.status_code == 403

    def test_set_library_inside_allowed_roots(self, client, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        state.config["allowed_roots"] = [str(allowed)]
        resp = client.post("/api/library", json={"path": str(allowed)})
        assert resp.status_code == 200

    def test_security_headers_present(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get("/api/config")
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"

    def test_exception_details_not_leaked(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        # Use a path inside the library that doesn't exist
        fake = os.path.join(library_with_pdfs, "nonexistent.pdf")
        resp = client.get(f"/api/pdf?path={fake}")
        assert resp.status_code == 404
        detail = resp.json().get("detail", "")
        assert library_with_pdfs not in detail

    def test_config_auth_salt_does_not_gate_the_api(self, client):
        """A leftover auth_salt in the config must not revive the 401 gate."""
        srv._save_config({"auth_salt": "testuser"})
        resp = client.get("/api/config")
        assert resp.status_code == 200

    def test_env_auth_salt_does_not_gate_the_api(self, client, monkeypatch):
        """A stale FOLIO_AUTH_SALT (e.g. left in compose) is inert."""
        monkeypatch.setenv("FOLIO_AUTH_SALT", "leftover-from-compose")
        resp = client.get("/api/config")
        assert resp.status_code == 200

    def test_auth_status_endpoint_removed(self, client):
        """Unrouted GETs fall through to the static mount, which 404s."""
        assert client.get("/api/auth-status").status_code == 404

    @pytest.mark.parametrize("path", ["/api/login", "/api/never-existed"])
    def test_login_endpoint_removed(self, client, path):
        """/api/login is indistinguishable from a path that never existed.

        Unrouted POSTs reach the static mount, which serves GET/HEAD only,
        so the honest response is 405 rather than 404.
        """
        resp = client.post(path, json={"passphrase": "anything"})
        assert resp.status_code == 405
        assert "folio_session" not in resp.cookies


# ---------------------------------------------------------------------------
# PUT /api/scores/tags
# ---------------------------------------------------------------------------


class TestGetScore:
    """GET /api/scores — authoritative per-score record for the tag editor."""

    def test_returns_folder_and_filename_tags_split(self, client, library_with_pdfs):
        """The split is the whole point: /api/recent only exposes combined tags."""
        state.set_library(library_with_pdfs)
        path = os.path.join(library_with_pdfs, "jazz", "Davis - Blue -- swing.pdf")
        resp = client.get(f"/api/scores?path={path}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["folder_tags"] == ["jazz"]
        assert data["filename_tags"] == ["swing"]
        assert "swing" in data["tags"] and "jazz" in data["tags"]

    def test_unknown_path_in_library_returns_404(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        missing = os.path.join(library_with_pdfs, "Nobody - Nothing.pdf")
        resp = client.get(f"/api/scores?path={missing}")
        assert resp.status_code == 404

    def test_no_library_set_returns_400(self, client, tmp_path):
        state.library_dir = ""
        state.scores = []
        resp = client.get(f"/api/scores?path={tmp_path / 'x.pdf'}")
        assert resp.status_code == 400

    def test_path_traversal_blocked(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get(
            f"/api/scores?path={library_with_pdfs}/../../../etc/passwd")
        assert resp.status_code in (403, 404)


class TestUpdateTags:
    def test_add_tag(self, client, library_with_pdfs):
        client.post("/api/library", json={"path": library_with_pdfs})
        resp = client.put("/api/scores/tags", json={
            "path": os.path.join(library_with_pdfs, "Bach - Cello Suite.pdf"),
            "filename_tags": ["jazz"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"]
        assert "jazz" in data["score"]["filename_tags"]
        assert data["score"]["filename"] == "Bach - Cello Suite -- jazz.pdf"
        # Old file gone, new file exists
        assert not os.path.exists(os.path.join(library_with_pdfs, "Bach - Cello Suite.pdf"))
        assert os.path.exists(os.path.join(library_with_pdfs, "Bach - Cello Suite -- jazz.pdf"))

    def test_remove_tag(self, client, library_with_pdfs):
        client.post("/api/library", json={"path": library_with_pdfs})
        tagged = os.path.join(library_with_pdfs, "jazz", "Davis - Blue -- swing.pdf")
        resp = client.put("/api/scores/tags", json={
            "path": tagged,
            "filename_tags": [],
        })
        assert resp.status_code == 200
        assert resp.json()["score"]["filename"] == "Davis - Blue.pdf"

    def test_folder_tags_preserved(self, client, library_with_pdfs):
        client.post("/api/library", json={"path": library_with_pdfs})
        tagged = os.path.join(library_with_pdfs, "jazz", "Davis - Blue -- swing.pdf")
        resp = client.put("/api/scores/tags", json={
            "path": tagged,
            "filename_tags": ["cool"],
        })
        assert resp.status_code == 200
        score = resp.json()["score"]
        assert "jazz" in score["folder_tags"]
        assert "cool" in score["filename_tags"]

    def test_no_library_returns_400(self, client):
        resp = client.put("/api/scores/tags", json={
            "path": "/some/file.pdf",
            "filename_tags": ["jazz"],
        })
        assert resp.status_code == 400

    def test_not_found_returns_404(self, client, library_with_pdfs):
        client.post("/api/library", json={"path": library_with_pdfs})
        resp = client.put("/api/scores/tags", json={
            "path": os.path.join(library_with_pdfs, "Nonexistent.pdf"),
            "filename_tags": ["jazz"],
        })
        assert resp.status_code in (403, 404)

    def test_setlist_references_updated(self, client, library_with_pdfs):
        client.post("/api/library", json={"path": library_with_pdfs})
        pdf_path = os.path.join(library_with_pdfs, "Bach - Cello Suite.pdf")
        # Create a setlist referencing this score
        client.post("/api/setlists", json={"name": "Test"})
        client.put("/api/setlists/Test", json={"items": [{
            "type": "song",
            "path": pdf_path,
            "title": "Cello Suite",
            "composer": "Bach",
            "start_page": 1,
            "end_page": None,
        }]})
        # Rename via tag update
        resp = client.put("/api/scores/tags", json={
            "path": pdf_path,
            "filename_tags": ["baroque"],
        })
        assert resp.status_code == 200
        new_path = resp.json()["score"]["filepath"]
        # Verify setlist now references the new path
        sl = client.get("/api/setlists/Test").json()
        assert sl["items"][0]["path"] == new_path

    def test_hash_index_follows_rename(self, client, library_with_pdfs):
        old_path = os.path.join(library_with_pdfs, "Bach - Cello Suite.pdf")
        # Unique content: duplicate-content PDFs are kept out of the index
        with open(old_path, "wb") as f:
            f.write(b"%PDF-1.4 unique cello")
        client.post("/api/library", json={"path": library_with_pdfs})
        resp = client.put("/api/scores/tags", json={
            "path": old_path, "filename_tags": ["baroque"],
        })
        assert resp.status_code == 200
        idx = load_hash_index(os.path.join(library_with_pdfs, "_hash_index.json"),
                              state.library_dir)
        new_path = resp.json()["score"]["filepath"]
        assert new_path in idx.values()
        assert srv.portable_path(old_path) not in idx.values()

    def test_non_object_hash_index_does_not_break_rename(self, client, library_with_pdfs):
        """Regression: a hash index of the wrong JSON shape gave a 500 after
        the file had already been renamed."""
        client.post("/api/library", json={"path": library_with_pdfs})
        with open(os.path.join(library_with_pdfs, "_hash_index.json"), "w") as f:
            f.write("[]")
        resp = client.put("/api/scores/tags", json={
            "path": os.path.join(library_with_pdfs, "Bach - Cello Suite.pdf"),
            "filename_tags": ["baroque"],
        })
        assert resp.status_code == 200

    def test_non_object_hash_index_does_not_break_rescan(self, client, library_with_pdfs):
        # Unique content, so the rescan has hashes and reads the old index
        with open(os.path.join(library_with_pdfs, "Bach - Cello Suite.pdf"), "wb") as f:
            f.write(b"%PDF-1.4 unique cello")
        with open(os.path.join(library_with_pdfs, "_hash_index.json"), "w") as f:
            f.write("[]")
        resp = client.post("/api/library", json={"path": library_with_pdfs})
        assert resp.status_code == 200

    def test_target_exists_returns_409(self, client, library_with_pdfs):
        client.post("/api/library", json={"path": library_with_pdfs})
        # Create a file that would collide
        target = os.path.join(library_with_pdfs, "Bach - Cello Suite -- jazz.pdf")
        with open(target, "wb") as f:
            f.write(b"%PDF-1.4 fake")
        resp = client.put("/api/scores/tags", json={
            "path": os.path.join(library_with_pdfs, "Bach - Cello Suite.pdf"),
            "filename_tags": ["jazz"],
        })
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Content hash and reference healing
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_library_response_includes_hash(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get("/api/library")
        scores = resp.json()["scores"]
        for s in scores:
            assert "content_hash" in s
            assert len(s["content_hash"]) == 12

    def test_tag_rename_preserves_hash(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        path = os.path.join(library_with_pdfs, "Bach - Cello Suite.pdf")
        orig_hash = next(
            s.content_hash for s in state.scores
            if "Bach" in s.composer
        )
        resp = client.put("/api/scores/tags", json={
            "path": path,
            "filename_tags": ["jazz"],
        })
        assert resp.status_code == 200
        assert resp.json()["score"]["content_hash"] == orig_hash


class TestHealReferences:
    def test_heal_setlist_after_external_rename(self, tmp_path):
        """Externally renaming a PDF heals setlist references on rescan."""
        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.write_bytes(b"%PDF-1.4 bach suite content")
        state.set_library(str(tmp_path))

        # Add to setlist
        from web.core import SafeJSON, portable_path
        setlist_data = {"My Set": [
            {"type": "song", "path": portable_path(str(pdf)),
             "title": "Suite", "composer": "Bach"},
        ]}
        SafeJSON.save(state.setlist_path(), setlist_data)

        # Rename externally
        new_pdf = tmp_path / "Bach - Cello Suite No 1.pdf"
        os.rename(str(pdf), str(new_pdf))

        # Rescan
        state.set_library(str(tmp_path))

        # Check setlist was healed (and migrated to new schema shape)
        data = load_setlists(state.setlist_path(), state.library_dir)
        assert data["My Set"]["items"][0]["path"] == portable_path(str(new_pdf))

    def test_heal_survives_malformed_setlist_and_recent_entries(self, tmp_path):
        """A stray non-dict item must not abort healing of the real ones."""
        from web.core import SafeJSON, portable_path
        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.write_bytes(b"%PDF-1.4 bach suite content")
        state.set_library(str(tmp_path))
        p = portable_path(str(pdf))
        SafeJSON.save(state.setlist_path(), {"My Set": {"items": [
            "junk", {"type": "song", "path": p, "title": "Suite", "composer": "Bach"},
        ], "shuffle": False}})
        SafeJSON.save(state.recent_path(), [None, {"filepath": p, "timestamp": 1}])

        new_pdf = tmp_path / "Bach - Cello Suite No 1.pdf"
        os.rename(str(pdf), str(new_pdf))
        state.set_library(str(tmp_path))

        new_p = portable_path(str(new_pdf))
        items = load_setlists(state.setlist_path(), state.library_dir)["My Set"]["items"]
        assert items[0] == "junk" and items[1]["path"] == new_p
        recent = load_recent(state.recent_path(), state.library_dir)
        assert recent[0] is None and recent[1]["filepath"] == new_p

    def test_heal_annotation_sidecar_after_rename(self, tmp_path):
        """Externally renaming a PDF moves its annotation sidecar."""
        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.write_bytes(b"%PDF-1.4 annotation test")
        sidecar = tmp_path / "Bach - Suite.json"
        sidecar.write_text('{"version":2,"pages":{},"rotations":{}}')
        state.set_library(str(tmp_path))

        # Rename externally
        new_pdf = tmp_path / "Bach - Suite No 1.pdf"
        os.rename(str(pdf), str(new_pdf))

        # Rescan
        state.set_library(str(tmp_path))

        # Old sidecar gone, new one exists
        assert not sidecar.exists()
        new_sidecar = tmp_path / "Bach - Suite No 1.json"
        assert new_sidecar.exists()

    def test_no_heal_when_paths_unchanged(self, tmp_path):
        """Rescan with no renames doesn't modify setlists."""
        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.write_bytes(b"%PDF-1.4 stable")
        state.set_library(str(tmp_path))

        from web.core import SafeJSON, portable_path
        setlist_data = {"My Set": [
            {"type": "song", "path": portable_path(str(pdf)),
             "title": "Suite", "composer": "Bach"},
        ]}
        SafeJSON.save(state.setlist_path(), setlist_data)
        state.set_library(str(tmp_path))  # one-time conversion to relative paths
        mtime = os.path.getmtime(state.setlist_path())

        # Rescan — no changes
        state.set_library(str(tmp_path))

        # Setlist file not rewritten
        assert os.path.getmtime(state.setlist_path()) == mtime

    def test_first_scan_creates_hash_index(self, tmp_path):
        """First scan creates _hash_index.json without errors."""
        (tmp_path / "score.pdf").write_bytes(b"%PDF-1.4 first")
        state.set_library(str(tmp_path))
        assert os.path.exists(state.hash_index_path())

    def test_duplicate_content_does_not_move_sidecar(self, tmp_path):
        """An identical copy must not pull another file's sidecar onto itself."""
        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.write_bytes(b"%PDF-1.4 duplicated content")
        sidecar = tmp_path / "Bach - Suite.json"
        sidecar.write_text('{"version":2,"pages":{},"rotations":{}}')
        state.set_library(str(tmp_path))

        # Subdirectories are always walked after top-level files, so the copy
        # is the one that would win a hash collision.
        copies = tmp_path / "copies"
        copies.mkdir()
        (copies / "Bach - Suite.pdf").write_bytes(b"%PDF-1.4 duplicated content")

        state.set_library(str(tmp_path))

        # The original is still on disk, so its sidecar must stay put.
        assert sidecar.exists()
        assert not (copies / "Bach - Suite.json").exists()

    def test_duplicate_content_does_not_rewrite_setlist(self, tmp_path):
        """An identical copy must not redirect setlist entries."""
        from web.core import SafeJSON, portable_path
        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.write_bytes(b"%PDF-1.4 duplicated setlist content")
        state.set_library(str(tmp_path))

        setlist_data = {"My Set": [
            {"type": "song", "path": portable_path(str(pdf)),
             "title": "Suite", "composer": "Bach"},
        ]}
        SafeJSON.save(state.setlist_path(), setlist_data)

        copies = tmp_path / "copies"
        copies.mkdir()
        (copies / "Bach - Suite.pdf").write_bytes(
            b"%PDF-1.4 duplicated setlist content")

        state.set_library(str(tmp_path))

        # Nothing was healed, so the entry still points at the original.
        data = load_setlists(state.setlist_path(), state.library_dir)
        assert data["My Set"]["items"][0]["path"] == portable_path(str(pdf))

    def test_rename_still_heals_alongside_duplicates(self, tmp_path):
        """Skipping ambiguous hashes must not block genuine rename healing."""
        dup = tmp_path / "Bach - Suite.pdf"
        dup.write_bytes(b"%PDF-1.4 duplicated")
        dup_sidecar = tmp_path / "Bach - Suite.json"
        dup_sidecar.write_text('{"version":2,"pages":{},"rotations":{}}')

        moved = tmp_path / "Mozart - Sonata.pdf"
        moved.write_bytes(b"%PDF-1.4 unique mozart")
        moved_sidecar = tmp_path / "Mozart - Sonata.json"
        moved_sidecar.write_text('{"version":2,"pages":{},"rotations":{}}')
        state.set_library(str(tmp_path))

        copies = tmp_path / "copies"
        copies.mkdir()
        (copies / "Bach - Suite.pdf").write_bytes(b"%PDF-1.4 duplicated")
        os.rename(str(moved), str(tmp_path / "Mozart - Sonata No 2.pdf"))

        state.set_library(str(tmp_path))

        # The genuine rename still heals ...
        assert not moved_sidecar.exists()
        assert (tmp_path / "Mozart - Sonata No 2.json").exists()
        # ... while the duplicated pair is left alone.
        assert dup_sidecar.exists()

    def test_unhashable_file_on_disk_is_not_remapped(self, tmp_path, monkeypatch):
        """A file whose hash cannot be computed is not treated as renamed away."""
        import web.core as core

        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.write_bytes(b"%PDF-1.4 unhashable content")
        sidecar = tmp_path / "Bach - Suite.json"
        sidecar.write_text('{"version":2,"pages":{},"rotations":{}}')
        state.set_library(str(tmp_path))

        # A copy carrying the original's hash appears, while the original stops
        # hashing but stays on disk.  Bump its mtime so the scan cache cannot
        # supply the previously computed hash.
        copies = tmp_path / "copies"
        copies.mkdir()
        (copies / "Bach - Suite.pdf").write_bytes(b"%PDF-1.4 unhashable content")
        os.utime(str(pdf), (0, 0))

        real_hash = core.compute_content_hash

        def fake_hash(filepath, size=None):
            if os.path.basename(os.path.dirname(filepath)) != "copies":
                return ""
            return real_hash(filepath, size=size)

        monkeypatch.setattr(core, "compute_content_hash", fake_hash)
        state.set_library(str(tmp_path))

        assert sidecar.exists()
        assert not (copies / "Bach - Suite.json").exists()


class TestHealReferencesRobustness:
    """The index must survive scans that carry no trustworthy information."""

    def test_empty_scan_keeps_existing_hash_index(self, tmp_path):
        """A library that scans to zero scores must not wipe the index."""
        from web.core import portable_path
        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.write_bytes(b"%PDF-1.4 present")
        state.set_library(str(tmp_path))
        before = SafeJSON.load(state.hash_index_path(), default={})
        assert before, "precondition: first scan records a hash"

        # The library disappears -- e.g. a volume that has not mounted yet.
        os.remove(str(pdf))
        state.set_library(str(tmp_path))

        assert SafeJSON.load(state.hash_index_path(), default={}) == before
        after = load_hash_index(state.hash_index_path(), state.library_dir)
        assert portable_path(str(pdf)) in after.values()

    def test_scan_without_usable_hashes_keeps_index(self, tmp_path, monkeypatch):
        """Scores present but all unhashable must not wipe the index either."""
        import web.core as core

        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.write_bytes(b"%PDF-1.4 unreadable later")
        state.set_library(str(tmp_path))
        before = SafeJSON.load(state.hash_index_path(), default={})
        assert before

        # Every read fails.  Bump mtime so the scan cache cannot supply the
        # previously computed hash.
        os.utime(str(pdf), (0, 0))
        monkeypatch.setattr(core, "compute_content_hash",
                            lambda filepath, size=None: "")
        state.set_library(str(tmp_path))

        assert SafeJSON.load(state.hash_index_path(), default={}) == before

    def test_empty_scan_keeps_scan_cache(self, tmp_path):
        """An empty scan must not clobber the scan cache either."""
        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.write_bytes(b"%PDF-1.4 cached")
        state.set_library(str(tmp_path))
        before = SafeJSON.load(state.scan_cache_path(), default={})
        assert before, "precondition: first scan records a cache entry"

        os.remove(str(pdf))
        state.set_library(str(tmp_path))

        assert SafeJSON.load(state.scan_cache_path(), default={}) == before

    def test_failed_heal_keeps_old_index_and_retries(self, tmp_path, monkeypatch):
        """A heal that cannot write must leave the index alone so it retries."""
        from web.core import SafeJSONError, portable_path

        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.write_bytes(b"%PDF-1.4 heal retry")
        state.set_library(str(tmp_path))

        old_portable = portable_path(str(pdf))
        SafeJSON.save(state.setlist_path(), {"My Set": [
            {"type": "song", "path": old_portable,
             "title": "Suite", "composer": "Bach"},
        ]})

        new_pdf = tmp_path / "Bach - Cello Suite.pdf"
        os.rename(str(pdf), str(new_pdf))

        # The setlist file cannot be written during this scan.  Restore by
        # re-patching, never monkeypatch.undo() -- reset_state shares this
        # monkeypatch instance, and undoing it would let _save_config write to
        # the real ~/.folio/web_config.json.
        real_save_setlists = srv._save_setlists

        def boom(data):
            raise SafeJSONError("read-only mount")

        monkeypatch.setattr(srv, "_save_setlists", boom)
        state.set_library(str(tmp_path))

        # set_library survived and still recorded the directory ...
        assert state.config["last_directory"] == portable_path(str(tmp_path))
        # ... and the index still points at the OLD path, so the remap recurs.
        idx = load_hash_index(state.hash_index_path(), state.library_dir)
        assert old_portable in idx.values()
        assert portable_path(str(new_pdf)) not in idx.values()

        # With writes working again, the next scan completes the heal.
        monkeypatch.setattr(srv, "_save_setlists", real_save_setlists)
        state.set_library(str(tmp_path))

        data = load_setlists(state.setlist_path(), state.library_dir)
        assert data["My Set"]["items"][0]["path"] == portable_path(str(new_pdf))


# ---------------------------------------------------------------------------
# Recent endpoints
# ---------------------------------------------------------------------------


class TestRecent:
    def test_empty_initially(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get("/api/recent")
        assert resp.status_code == 200
        assert resp.json() == {"recent": []}

    def test_add_and_list(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        score = state.scores[0]
        resp = client.post("/api/recent", json={"path": score.filepath})
        assert resp.status_code == 200
        data = client.get("/api/recent").json()["recent"]
        assert len(data) == 1
        assert data[0]["composer"] == score.composer
        assert data[0]["title"] == score.title
        assert data[0]["content_hash"] == score.content_hash

    def test_add_dedupes_and_promotes(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        a, b = state.scores[0], state.scores[1]
        client.post("/api/recent", json={"path": a.filepath})
        client.post("/api/recent", json={"path": b.filepath})
        client.post("/api/recent", json={"path": a.filepath})
        data = client.get("/api/recent").json()["recent"]
        assert len(data) == 2
        assert data[0]["title"] == a.title  # most recent first

    def test_add_caps_at_max(self, client, library_with_pdfs, monkeypatch):
        monkeypatch.setattr(srv, "MAX_RECENT", 2)
        state.set_library(library_with_pdfs)
        for s in state.scores[:3]:
            client.post("/api/recent", json={"path": s.filepath})
        data = client.get("/api/recent").json()["recent"]
        assert len(data) == 2

    def test_unknown_path_returns_404(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.post(
            "/api/recent",
            json={"path": os.path.join(library_with_pdfs, "nope.pdf")},
        )
        assert resp.status_code == 404

    def test_traversal_blocked(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.post("/api/recent", json={"path": "/etc/passwd"})
        assert resp.status_code in (400, 403, 404)

    def test_clear(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/recent", json={"path": state.scores[0].filepath})
        resp = client.delete("/api/recent")
        assert resp.status_code == 200
        assert client.get("/api/recent").json() == {"recent": []}

    def test_heals_on_rename_via_api(self, client, library_with_pdfs):
        """Renaming via /api/scores/tags updates the recent entry's filepath."""
        state.set_library(library_with_pdfs)
        score = state.scores[0]
        client.post("/api/recent", json={"path": score.filepath})
        resp = client.put(
            "/api/scores/tags",
            json={"path": score.filepath, "filename_tags": ["renamed"]},
        )
        assert resp.status_code == 200
        new_path = resp.json()["score"]["filepath"]
        data = client.get("/api/recent").json()["recent"]
        assert len(data) == 1
        assert data[0]["filepath"].endswith(os.path.basename(new_path))

    def test_persists_across_set_library(self, client, library_with_pdfs):
        """Recent entries survive a rescan."""
        state.set_library(library_with_pdfs)
        client.post("/api/recent", json={"path": state.scores[0].filepath})
        state.set_library(library_with_pdfs)
        data = client.get("/api/recent").json()["recent"]
        assert len(data) == 1

    def test_includes_current_tags(self, client, library_with_pdfs):
        """Recent entries are enriched with the score's live tags."""
        state.set_library(library_with_pdfs)
        tagged = next(s for s in state.scores if s.tags)
        client.post("/api/recent", json={"path": tagged.filepath})
        data = client.get("/api/recent").json()["recent"]
        assert data[0]["tags"] == sorted(tagged.tags)

    def test_tags_reflect_rename(self, client, library_with_pdfs):
        """Retagging a score updates the tags surfaced by /api/recent."""
        state.set_library(library_with_pdfs)
        score = state.scores[0]
        client.post("/api/recent", json={"path": score.filepath})
        client.put(
            "/api/scores/tags",
            json={"path": score.filepath, "filename_tags": ["fresh"]},
        )
        data = client.get("/api/recent").json()["recent"]
        assert data[0]["tags"] == ["fresh"]

    def test_missing_score_gets_empty_tags(self, client, library_with_pdfs):
        """Entries no longer in the library still list with empty tags."""
        state.set_library(library_with_pdfs)
        score = state.scores[0]
        client.post("/api/recent", json={"path": score.filepath})
        os.remove(score.filepath)
        state.set_library(library_with_pdfs)
        data = client.get("/api/recent").json()["recent"]
        assert len(data) == 1
        assert data[0]["tags"] == []


# ---------------------------------------------------------------------------
# GET /api/newest
# ---------------------------------------------------------------------------


class TestNewest:
    def test_empty_library(self, client):
        resp = client.get("/api/newest")
        assert resp.status_code == 200
        assert resp.json() == {"scores": [], "total": 0}

    def test_orders_by_mtime_desc(self, client, library_with_pdfs):
        # Stamp distinct mtimes before scanning so order is deterministic.
        import os as _os
        names = [
            "Bach - Cello Suite.pdf",
            "Mozart - Sonata.pdf",
            _os.path.join("jazz", "Davis - Blue -- swing.pdf"),
        ]
        for i, name in enumerate(names):
            ts = 1_000_000 + i * 1000
            _os.utime(_os.path.join(library_with_pdfs, name), (ts, ts))
        state.set_library(library_with_pdfs)

        scores = client.get("/api/newest").json()["scores"]
        titles = [s["title"] for s in scores]
        assert titles == ["Blue", "Sonata", "Cello Suite"]

    def test_includes_tags_and_mtime(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        scores = client.get("/api/newest").json()["scores"]
        swing = next(s for s in scores if s["title"] == "Blue")
        assert swing["tags"] == ["jazz", "swing"]  # folder + filename tag
        assert swing["mtime"] > 0

    def test_limit_clamped_and_applied(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        data = client.get("/api/newest?limit=2").json()
        assert data["total"] == 2
        assert len(data["scores"]) == 2
        # Out-of-range limits are rejected by validation.
        assert client.get("/api/newest?limit=0").status_code == 422
        assert client.get("/api/newest?limit=999").status_code == 422


# ---------------------------------------------------------------------------
# Shuffle setlists
# ---------------------------------------------------------------------------


class TestShuffleSetlist:
    def test_new_setlist_shuffle_false(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "S"})
        resp = client.get("/api/setlists/S")
        assert resp.json()["shuffle"] is False

    def test_set_shuffle_true(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "S"})
        resp = client.post("/api/setlists/S/shuffle", json={"shuffle": True})
        assert resp.status_code == 200
        assert resp.json()["shuffle"] is True
        assert client.get("/api/setlists/S").json()["shuffle"] is True

    def test_shuffle_included_in_listing(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "A"})
        client.post("/api/setlists/A/shuffle", json={"shuffle": True})
        client.post("/api/setlists", json={"name": "B"})
        data = {sl["name"]: sl for sl in client.get("/api/setlists").json()["setlists"]}
        assert data["A"]["shuffle"] is True
        assert data["B"]["shuffle"] is False

    def test_shuffle_setlist_returns_404(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.post("/api/setlists/Nope/shuffle", json={"shuffle": True})
        assert resp.status_code == 404

    def test_playback_no_shuffle_is_stable(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "S"})
        items = [_song(f"T{i}") for i in range(8)]
        client.put("/api/setlists/S", json={"items": items})
        a = client.get("/api/setlists/S/playback").json()["songs"]
        b = client.get("/api/setlists/S/playback").json()["songs"]
        assert [s["title"] for s in a] == [s["title"] for s in b]

    def test_playback_with_shuffle_permutes(self, client, library_with_pdfs):
        """With shuffle=True, multiple calls eventually produce different orders."""
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "S"})
        items = [_song(f"T{i}") for i in range(8)]
        client.put("/api/setlists/S", json={"items": items})
        client.post("/api/setlists/S/shuffle", json={"shuffle": True})
        baseline = [s["title"] for s in
                    client.get("/api/setlists/S/playback").json()["songs"]]
        # Same elements every time
        for _ in range(20):
            got = [s["title"] for s in
                   client.get("/api/setlists/S/playback").json()["songs"]]
            assert sorted(got) == sorted(baseline)
        # At least one of many calls must differ
        diffs = 0
        for _ in range(20):
            got = [s["title"] for s in
                   client.get("/api/setlists/S/playback").json()["songs"]]
            if got != baseline:
                diffs += 1
        assert diffs > 0

    def test_playback_nested_shuffle_within_shuffled_parent(
        self, client, library_with_pdfs,
    ):
        """When parent is shuffled and child ref is also shuffled, both shuffle."""
        state.set_library(library_with_pdfs)
        # Child setlist with 4 songs, shuffle=on
        client.post("/api/setlists", json={"name": "Child"})
        client.put(
            "/api/setlists/Child",
            json={"items": [_song(f"C{i}") for i in range(4)]},
        )
        client.post("/api/setlists/Child/shuffle", json={"shuffle": True})
        # Parent with [ref to Child, P0, P1], shuffle=on
        client.post("/api/setlists", json={"name": "Parent"})
        client.put(
            "/api/setlists/Parent",
            json={"items": [_ref("Child"), _song("P0"), _song("P1")]},
        )
        client.post("/api/setlists/Parent/shuffle", json={"shuffle": True})
        for _ in range(10):
            songs = client.get("/api/setlists/Parent/playback").json()["songs"]
            titles = [s["title"] for s in songs]
            assert sorted(titles) == sorted(["C0", "C1", "C2", "C3", "P0", "P1"])

    def test_playback_nested_ref_unshuffled_keeps_order(
        self, client, library_with_pdfs,
    ):
        """A ref to a non-shuffled child must play its songs in order."""
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "Child"})
        client.put(
            "/api/setlists/Child",
            json={"items": [_song(f"C{i}") for i in range(4)]},
        )
        # Child shuffle stays False
        client.post("/api/setlists", json={"name": "Parent"})
        client.put("/api/setlists/Parent", json={"items": [_ref("Child")]})
        # Parent shuffle stays False too
        songs = client.get("/api/setlists/Parent/playback").json()["songs"]
        assert [s["title"] for s in songs] == ["C0", "C1", "C2", "C3"]

    def test_flat_endpoint_unaffected_by_shuffle(self, client, library_with_pdfs):
        """/flat stays deterministic even when shuffle=True (used by Cache)."""
        state.set_library(library_with_pdfs)
        client.post("/api/setlists", json={"name": "S"})
        items = [_song(f"T{i}") for i in range(8)]
        client.put("/api/setlists/S", json={"items": items})
        client.post("/api/setlists/S/shuffle", json={"shuffle": True})
        a = client.get("/api/setlists/S/flat").json()["songs"]
        b = client.get("/api/setlists/S/flat").json()["songs"]
        assert [s["title"] for s in a] == [s["title"] for s in b] == [
            f"T{i}" for i in range(8)
        ]

    def test_legacy_list_shape_loads(self, client, library_with_pdfs):
        """A setlist saved in the old list-of-items shape loads transparently."""
        state.set_library(library_with_pdfs)
        from web.core import SafeJSON
        SafeJSON.save(
            state.setlist_path(),
            {"Old": [_song("X")]},  # old shape on disk
        )
        resp = client.get("/api/setlists/Old")
        assert resp.status_code == 200
        body = resp.json()
        assert body["shuffle"] is False
        assert len(body["items"]) == 1
