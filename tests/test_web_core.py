"""Tests for web.core — business logic extracted for the web backend."""

import json
import os
import tempfile

import pytest

from web.core import (
    SafeJSON,
    SafeJSONError,
    Score,
    normalize_path,
    portable_path,
    scan_library,
)


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------


class TestNormalizePath:
    def test_empty_string(self):
        assert normalize_path("") == ""

    def test_forward_slashes_preserved_on_linux(self):
        result = normalize_path("/mnt/z/Music/score.pdf")
        assert "\\" not in result


class TestPortablePath:
    def test_empty_string(self):
        assert portable_path("") == ""

    def test_backslashes_converted(self):
        assert portable_path("Z:\\Music\\score.pdf") == "Z:/Music/score.pdf"


# ---------------------------------------------------------------------------
# SafeJSON
# ---------------------------------------------------------------------------


class TestSafeJSONLoad:
    def test_missing_file_returns_default(self):
        assert SafeJSON.load("/nonexistent/file.json") == {}

    def test_missing_file_custom_default(self):
        assert SafeJSON.load("/nonexistent/file.json", default=[]) == []

    def test_valid_json(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text('{"key": "value"}')
        assert SafeJSON.load(str(p)) == {"key": "value"}

    def test_corrupt_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{invalid json")
        with pytest.raises(SafeJSONError, match="Corrupt JSON"):
            SafeJSON.load(str(p))


class TestSafeJSONSave:
    def test_save_and_reload(self, tmp_path):
        p = tmp_path / "out.json"
        data = {"hello": "world", "n": 42}
        SafeJSON.save(str(p), data)
        loaded = json.loads(p.read_text())
        assert loaded == data

    def test_save_missing_directory_raises(self):
        with pytest.raises(SafeJSONError, match="directory does not exist"):
            SafeJSON.save("/nonexistent/dir/file.json", {})

    def test_overwrite_existing(self, tmp_path):
        p = tmp_path / "data.json"
        SafeJSON.save(str(p), {"v": 1})
        SafeJSON.save(str(p), {"v": 2})
        assert json.loads(p.read_text()) == {"v": 2}


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------


class TestScore:
    def test_composer_title_parsing(self):
        s = Score("/music/Bach - Cello Suite.pdf", "Bach - Cello Suite.pdf")
        assert s.composer == "Bach"
        assert s.title == "Cello Suite"

    def test_title_only(self):
        s = Score("/music/MyScore.pdf", "MyScore.pdf")
        assert s.composer == "Unknown"
        assert s.title == "MyScore"

    def test_tags_from_filename(self):
        s = Score("/music/Bach - Suite -- jazz blues.pdf",
                  "Bach - Suite -- jazz blues.pdf")
        assert "jazz" in s.tags
        assert "blues" in s.tags

    def test_folder_tags(self):
        s = Score("/music/classical/Bach - Suite.pdf",
                  "Bach - Suite.pdf", folder_tags={"classical"})
        assert "classical" in s.tags

    def test_to_dict(self):
        s = Score("/music/Bach - Suite.pdf", "Bach - Suite.pdf")
        d = s.to_dict()
        assert d["composer"] == "Bach"
        assert d["title"] == "Suite"
        assert isinstance(d["tags"], list)


# ---------------------------------------------------------------------------
# scan_library
# ---------------------------------------------------------------------------


class TestScanLibrary:
    def test_scan_finds_pdfs(self, tmp_path):
        (tmp_path / "score1.pdf").touch()
        (tmp_path / "score2.pdf").touch()
        (tmp_path / "readme.txt").touch()
        result = scan_library(str(tmp_path))
        assert len(result) == 2
        titles = {s.title for s in result}
        assert "score1" in titles
        assert "score2" in titles

    def test_scan_recursive(self, tmp_path):
        sub = tmp_path / "classical"
        sub.mkdir()
        (sub / "Bach - Suite.pdf").touch()
        result = scan_library(str(tmp_path))
        assert len(result) == 1
        assert "classical" in result[0].tags

    def test_scan_nonexistent_raises(self):
        with pytest.raises(FileNotFoundError):
            scan_library("/nonexistent/path")

    def test_scan_empty_dir(self, tmp_path):
        assert scan_library(str(tmp_path)) == []
