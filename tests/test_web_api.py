"""Tests for web.server — FastAPI endpoints."""

import os
import json

import pytest
from fastapi.testclient import TestClient

from web.server import app, state


@pytest.fixture(autouse=True)
def reset_state():
    """Reset server state before each test."""
    state.library_dir = ""
    state.scores = []
    yield
    state.library_dir = ""
    state.scores = []


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
