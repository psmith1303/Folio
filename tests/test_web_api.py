"""Tests for web.server — FastAPI endpoints."""

import os
import json

import pytest
from fastapi.testclient import TestClient

import web.server as srv
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
    yield
    state.library_dir = ""
    state.scores = []


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def library_with_real_pdf(tmp_path):
    """Create a temp directory with a real PDF for pymupdf-dependent tests."""
    import pymupdf as fitz
    doc = fitz.open()
    doc.new_page(width=612, height=792)
    doc.save(str(tmp_path / "Bach - Test Score.pdf"))
    doc.close()
    return str(tmp_path)


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

    def test_text_search(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get("/api/library?q=bach")
        data = resp.json()
        assert data["total"] == 1
        assert data["scores"][0]["composer"] == "Bach"

    def test_composer_filter(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get("/api/library?composer=Mozart")
        data = resp.json()
        assert data["total"] == 1
        assert data["scores"][0]["composer"] == "Mozart"

    def test_tag_filter(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get("/api/library?tag=jazz")
        data = resp.json()
        assert data["total"] == 1
        assert data["scores"][0]["composer"] == "Davis"

    def test_returns_available_composers(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get("/api/library")
        data = resp.json()
        assert "Bach" in data["composers"]
        assert "Mozart" in data["composers"]

    def test_sort_by_title(self, client, library_with_pdfs):
        state.set_library(library_with_pdfs)
        resp = client.get("/api/library?sort=title")
        titles = [s["title"] for s in resp.json()["scores"]]
        assert titles == sorted(titles, key=str.lower)


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
        assert resp.json()["songs"] == []

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
        assert len(resp.json()["songs"]) == 1
        assert resp.json()["songs"][0]["title"] == "A"

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


# ---------------------------------------------------------------------------
# PDF export
# ---------------------------------------------------------------------------


class TestExportPDF:
    def test_export_unannotated(self, client, library_with_real_pdf):
        state.set_library(library_with_real_pdf)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]
        resp = client.get(f"/api/pdf/export?path={path}")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content[:5] == b"%PDF-"

    def test_export_with_ink_annotation(self, client, library_with_real_pdf):
        state.set_library(library_with_real_pdf)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]

        pages = {"0": [{"uuid": "ink-1", "type": "ink",
                        "points": [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
                        "color": "red", "width": 3}]}
        client.put("/api/annotations", json={
            "path": path, "pages": pages, "rotations": {}
        })

        resp = client.get(f"/api/pdf/export?path={path}")
        assert resp.status_code == 200
        assert len(resp.content) > 100

    def test_export_with_text_annotation(self, client, library_with_real_pdf):
        state.set_library(library_with_real_pdf)
        scores = client.get("/api/library").json()["scores"]
        path = scores[0]["filepath"]

        pages = {"0": [{"uuid": "txt-1", "type": "text",
                        "x": 0.5, "y": 0.5, "text": "ff",
                        "color": "blue", "size": 4, "font": "serif"}]}
        client.put("/api/annotations", json={
            "path": path, "pages": pages, "rotations": {}
        })

        resp = client.get(f"/api/pdf/export?path={path}")
        assert resp.status_code == 200
        assert len(resp.content) > 100

    def test_export_no_library_returns_400(self, client):
        resp = client.get("/api/pdf/export?path=/some/file.pdf")
        assert resp.status_code == 400
