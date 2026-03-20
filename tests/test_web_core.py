"""Tests for web.core — business logic extracted for the web backend."""

import json
import os
import tempfile

import pytest

from web.core import (
    ANNOTATION_VERSION,
    AnnotationConflictError,
    SafeJSON,
    SafeJSONError,
    Score,
    annotation_sidecar_path,
    annotations_etag,
    load_annotations,
    normalize_path,
    portable_path,
    save_annotations,
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

    def test_exclude_file_skips_directory(self, tmp_path):
        """A directory with a .exclude file is skipped entirely."""
        included = tmp_path / "included"
        included.mkdir()
        (included / "keep.pdf").touch()
        excluded = tmp_path / "excluded"
        excluded.mkdir()
        (excluded / ".exclude").touch()
        (excluded / "hidden.pdf").touch()
        result = scan_library(str(tmp_path))
        assert len(result) == 1
        assert result[0].title == "keep"

    def test_exclude_file_skips_subdirectories(self, tmp_path):
        """A .exclude marker prevents recursion into subdirectories."""
        parent = tmp_path / "parent"
        parent.mkdir()
        (parent / ".exclude").touch()
        child = parent / "child"
        child.mkdir()
        (child / "deep.pdf").touch()
        result = scan_library(str(tmp_path))
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


class TestAnnotationSidecarPath:
    def test_derives_json_from_pdf(self):
        result = annotation_sidecar_path("/music/score.pdf")
        assert result.endswith("/score.json")

    def test_preserves_directory(self):
        result = annotation_sidecar_path("/music/sub/score.pdf")
        assert "/sub/" in result


class TestLoadAnnotations:
    def test_no_sidecar_returns_empty(self, tmp_path):
        pdf = tmp_path / "score.pdf"
        pdf.touch()
        data = load_annotations(str(pdf))
        assert data["version"] == ANNOTATION_VERSION
        assert data["pages"] == {}
        assert data["rotations"] == {}

    def test_loads_existing_sidecar(self, tmp_path):
        pdf = tmp_path / "score.pdf"
        pdf.touch()
        sidecar = tmp_path / "score.json"
        sidecar.write_text(json.dumps({
            "version": 2,
            "rotations": {"0": 90},
            "pages": {"0": [
                {"uuid": "abc", "type": "ink", "points": [[0.1, 0.2]],
                 "color": "red", "width": 3}
            ]}
        }))
        data = load_annotations(str(pdf))
        assert data["rotations"]["0"] == 90
        assert len(data["pages"]["0"]) == 1
        assert data["pages"]["0"][0]["uuid"] == "abc"

    def test_assigns_missing_uuids(self, tmp_path):
        pdf = tmp_path / "score.pdf"
        pdf.touch()
        sidecar = tmp_path / "score.json"
        sidecar.write_text(json.dumps({
            "version": 2,
            "rotations": {},
            "pages": {"0": [
                {"type": "ink", "points": [[0.5, 0.5]], "color": "black", "width": 2}
            ]}
        }))
        data = load_annotations(str(pdf))
        assert "uuid" in data["pages"]["0"][0]
        assert len(data["pages"]["0"][0]["uuid"]) > 0

    def test_migrates_old_format(self, tmp_path):
        pdf = tmp_path / "score.pdf"
        pdf.touch()
        sidecar = tmp_path / "score.json"
        # Old format: page numbers as top-level keys
        sidecar.write_text(json.dumps({
            "0": [{"type": "ink", "points": [[0.1, 0.2]], "color": "blue", "width": 1}]
        }))
        data = load_annotations(str(pdf))
        assert data["version"] == ANNOTATION_VERSION
        assert "0" in data["pages"]
        assert data["pages"]["0"][0]["type"] == "ink"


class TestAnnotationsEtag:
    def test_no_sidecar_returns_empty(self, tmp_path):
        pdf = tmp_path / "score.pdf"
        pdf.touch()
        assert annotations_etag(str(pdf)) == ""

    def test_etag_changes_after_save(self, tmp_path):
        pdf = tmp_path / "score.pdf"
        pdf.touch()
        save_annotations(str(pdf), {}, {})
        etag1 = annotations_etag(str(pdf))
        assert etag1 != ""

        save_annotations(str(pdf), {"0": [{"uuid": "a", "type": "ink",
                         "points": [[0.1, 0.2]], "color": "red", "width": 1}]}, {})
        etag2 = annotations_etag(str(pdf))
        assert etag2 != etag1

    def test_load_annotations_includes_etag(self, tmp_path):
        pdf = tmp_path / "score.pdf"
        pdf.touch()
        save_annotations(str(pdf), {}, {})
        data = load_annotations(str(pdf))
        assert "etag" in data
        assert len(data["etag"]) > 0


class TestSaveAnnotations:
    def test_save_and_reload(self, tmp_path):
        pdf = tmp_path / "score.pdf"
        pdf.touch()
        pages = {"0": [{"uuid": "xyz", "type": "text", "x": 0.5, "y": 0.5,
                        "text": "ff", "font": "serif", "color": "red", "size": 3}]}
        rotations = {"0": 90, "1": 0}
        save_annotations(str(pdf), pages, rotations)

        data = load_annotations(str(pdf))
        assert data["pages"]["0"][0]["text"] == "ff"
        # Rotation 0 should be filtered out
        assert "1" not in data["rotations"]
        assert data["rotations"]["0"] == 90

    def test_save_returns_new_etag(self, tmp_path):
        pdf = tmp_path / "score.pdf"
        pdf.touch()
        etag = save_annotations(str(pdf), {}, {})
        assert etag != ""

    def test_save_with_correct_etag_succeeds(self, tmp_path):
        pdf = tmp_path / "score.pdf"
        pdf.touch()
        etag = save_annotations(str(pdf), {}, {})
        # Save again with the correct etag
        new_etag = save_annotations(str(pdf), {}, {"0": 90}, expected_etag=etag)
        assert new_etag != etag

    def test_save_with_stale_etag_raises(self, tmp_path):
        pdf = tmp_path / "score.pdf"
        pdf.touch()
        etag = save_annotations(str(pdf), {}, {})
        # Simulate concurrent edit
        save_annotations(str(pdf), {"0": []}, {})
        # Now try to save with the stale etag
        with pytest.raises(AnnotationConflictError):
            save_annotations(str(pdf), {}, {}, expected_etag=etag)

    def test_save_without_etag_always_succeeds(self, tmp_path):
        pdf = tmp_path / "score.pdf"
        pdf.touch()
        save_annotations(str(pdf), {}, {})
        # Save without etag — no conflict check
        save_annotations(str(pdf), {"0": []}, {})
