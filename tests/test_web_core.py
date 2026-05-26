"""Tests for web.core — business logic extracted for the web backend."""

import json
import os

import pytest

from web.core import (
    ANNOTATION_VERSION,
    AnnotationConflictError,
    BAKED_TAG,
    SafeJSON,
    SafeJSONError,
    Score,
    annotation_sidecar_path,
    annotations_etag,
    bake_score,
    build_tagged_filename,
    compute_content_hash,
    export_annotated_pdf,
    load_annotations,
    normalize_path,
    portable_path,
    rename_score_tags,
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


class TestComputeContentHash:
    def test_returns_12_hex_chars(self, tmp_path):
        f = tmp_path / "test.pdf"
        f.write_bytes(b"%PDF-1.4 some content here")
        h = compute_content_hash(str(f))
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_content_same_hash(self, tmp_path):
        content = b"%PDF-1.4 identical"
        f1 = tmp_path / "a.pdf"
        f2 = tmp_path / "b.pdf"
        f1.write_bytes(content)
        f2.write_bytes(content)
        assert compute_content_hash(str(f1)) == compute_content_hash(str(f2))

    def test_different_content_different_hash(self, tmp_path):
        f1 = tmp_path / "a.pdf"
        f2 = tmp_path / "b.pdf"
        f1.write_bytes(b"%PDF-1.4 content A")
        f2.write_bytes(b"%PDF-1.4 content B")
        assert compute_content_hash(str(f1)) != compute_content_hash(str(f2))

    def test_nonexistent_file_returns_empty(self):
        assert compute_content_hash("/nonexistent/file.pdf") == ""

    def test_stable_after_rename(self, tmp_path):
        f = tmp_path / "original.pdf"
        f.write_bytes(b"%PDF-1.4 data")
        h1 = compute_content_hash(str(f))
        renamed = tmp_path / "renamed.pdf"
        os.rename(str(f), str(renamed))
        h2 = compute_content_hash(str(renamed))
        assert h1 == h2

    def test_large_file(self, tmp_path):
        """Files larger than 8KB use head + tail."""
        f = tmp_path / "big.pdf"
        f.write_bytes(b"A" * 5000 + b"B" * 5000)
        h = compute_content_hash(str(f))
        assert len(h) == 12


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
        assert "content_hash" in d


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

    def test_scan_populates_content_hash(self, tmp_path):
        (tmp_path / "score.pdf").write_bytes(b"%PDF-1.4 data")
        result = scan_library(str(tmp_path))
        assert len(result) == 1
        assert len(result[0].content_hash) == 12

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

    def test_hash_cache_reuse_skips_recompute(self, tmp_path, monkeypatch):
        """Files unchanged by size+mtime reuse the cached hash."""
        from web import core

        f = tmp_path / "score.pdf"
        f.write_bytes(b"%PDF-1.4 original")
        cache: dict = {}
        first = scan_library(str(tmp_path), hash_cache=cache)
        assert len(first) == 1
        assert len(cache) == 1

        calls: list[str] = []
        orig = core.compute_content_hash

        def spy(path, size=None):
            calls.append(path)
            return orig(path, size=size)

        monkeypatch.setattr(core, "compute_content_hash", spy)
        second = scan_library(str(tmp_path), hash_cache=cache)
        assert calls == []  # nothing recomputed
        assert second[0].content_hash == first[0].content_hash

    def test_hash_cache_invalidates_on_mtime_change(self, tmp_path):
        """Changing a file's mtime forces a fresh hash."""
        f = tmp_path / "score.pdf"
        f.write_bytes(b"%PDF-1.4 a")
        cache: dict = {}
        scan_library(str(tmp_path), hash_cache=cache)
        old = next(iter(cache.values()))

        f.write_bytes(b"%PDF-1.4 b changed")
        # Force an mtime newer than the cached value
        os.utime(str(f), (old["mtime"] + 10, old["mtime"] + 10))
        scan_library(str(tmp_path), hash_cache=cache)
        new = next(iter(cache.values()))
        assert new["mtime"] != old["mtime"]
        assert new["hash"] != old["hash"]

    def test_hash_cache_prunes_deleted_files(self, tmp_path):
        """Files removed from disk drop out of the cache."""
        f1 = tmp_path / "a.pdf"
        f2 = tmp_path / "b.pdf"
        f1.write_bytes(b"%PDF-1.4 one")
        f2.write_bytes(b"%PDF-1.4 two")
        cache: dict = {}
        scan_library(str(tmp_path), hash_cache=cache)
        assert len(cache) == 2

        f2.unlink()
        scan_library(str(tmp_path), hash_cache=cache)
        assert len(cache) == 1
        assert all("a.pdf" in k for k in cache)


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


# ---------------------------------------------------------------------------
# export_annotated_pdf — intrinsic /Rotate handling
# ---------------------------------------------------------------------------


class TestExportRotation:
    """Annotations are stored in canonical (post-intrinsic-rotation) normalized
    coords. The export must place them at the same canonical position regardless
    of the page's intrinsic /Rotate value."""

    @staticmethod
    def _make_pdf(path: str, rotation: int) -> None:
        import pymupdf as fitz

        doc = fitz.open()
        page = doc.new_page(width=400, height=600)
        page.set_rotation(rotation)
        doc.save(path)
        doc.close()

    @staticmethod
    def _find_red(pix) -> tuple[int, int] | None:
        samples = pix.samples
        n = pix.n
        for y in range(pix.height):
            for x in range(pix.width):
                i = (y * pix.width + x) * n
                r, g, b = samples[i], samples[i + 1], samples[i + 2]
                if r > 200 and g < 100 and b < 100:
                    return (x, y)
        return None

    @pytest.mark.parametrize("rotation", [0, 90, 180, 270])
    def test_ink_lands_at_canonical_position(self, tmp_path, rotation):
        import pymupdf as fitz

        pdf = tmp_path / "rotated.pdf"
        self._make_pdf(str(pdf), rotation)

        # Short red horizontal stroke at canonical (0.1, 0.1) — top-left area.
        ink = {
            "uuid": "ink-rot",
            "type": "ink",
            "color": "red",
            "width": 8,
            "points": [[0.10, 0.10], [0.12, 0.10], [0.14, 0.10]],
        }
        save_annotations(str(pdf), {"0": [ink]}, {})

        out_pdf = tmp_path / "out.pdf"
        out_pdf.write_bytes(export_annotated_pdf(str(pdf)))

        with fitz.open(str(out_pdf)) as doc:
            pix = doc[0].get_pixmap(dpi=72)

        red = self._find_red(pix)
        assert red is not None, f"No red pixel found for /Rotate {rotation}"
        # Expected canonical position; allow 20px slop for stroke thickness.
        ex_x, ex_y = 0.10 * pix.width, 0.10 * pix.height
        assert abs(red[0] - ex_x) < 20, (
            f"/Rotate {rotation}: x off — got {red[0]}, expected ~{ex_x:.0f}"
        )
        assert abs(red[1] - ex_y) < 20, (
            f"/Rotate {rotation}: y off — got {red[1]}, expected ~{ex_y:.0f}"
        )

    @pytest.mark.parametrize("rotation", [0, 90, 180, 270])
    def test_text_lands_at_canonical_position(self, tmp_path, rotation):
        """Text annotation centered at canonical (0.5, 0.5) appears upright at
        the canonical center after export, regardless of intrinsic rotation."""
        import pymupdf as fitz

        pdf = tmp_path / "rotated.pdf"
        self._make_pdf(str(pdf), rotation)

        text = {
            "uuid": "txt-rot",
            "type": "text",
            "x": 0.5, "y": 0.5,
            "text": "X",
            "color": "black",
            "size": 5,
            "font": "sans-serif",
        }
        save_annotations(str(pdf), {"0": [text]}, {})

        out_pdf = tmp_path / "out.pdf"
        out_pdf.write_bytes(export_annotated_pdf(str(pdf)))

        with fitz.open(str(out_pdf)) as doc:
            pix = doc[0].get_pixmap(dpi=72)

        # Find the dark glyph pixels and compute their centroid.
        samples = pix.samples
        n = pix.n
        xs, ys = [], []
        for y in range(pix.height):
            for x in range(pix.width):
                i = (y * pix.width + x) * n
                if samples[i] < 80 and samples[i + 1] < 80 and samples[i + 2] < 80:
                    xs.append(x)
                    ys.append(y)
        assert xs, f"No glyph pixels found for /Rotate {rotation}"
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        ex_x, ex_y = 0.5 * pix.width, 0.5 * pix.height
        assert abs(cx - ex_x) < 30, (
            f"/Rotate {rotation}: text x off — got {cx:.0f}, expected ~{ex_x:.0f}"
        )
        assert abs(cy - ex_y) < 30, (
            f"/Rotate {rotation}: text y off — got {cy:.0f}, expected ~{ex_y:.0f}"
        )


# ---------------------------------------------------------------------------
# build_tagged_filename
# ---------------------------------------------------------------------------


class TestBuildTaggedFilename:
    def test_composer_title_tags(self):
        result = build_tagged_filename("Bach", "Suite", {"jazz", "blues"})
        assert result == "Bach - Suite -- blues jazz.pdf"

    def test_composer_title_no_tags(self):
        assert build_tagged_filename("Bach", "Suite", set()) == "Bach - Suite.pdf"

    def test_unknown_composer(self):
        assert build_tagged_filename("Unknown", "MyScore", {"jazz"}) == "MyScore -- jazz.pdf"

    def test_empty_composer(self):
        assert build_tagged_filename("", "MyScore", set()) == "MyScore.pdf"

    def test_tags_sorted(self):
        result = build_tagged_filename("Bach", "Suite", {"zebra", "alpha", "mid"})
        assert " -- alpha mid zebra.pdf" in result

    def test_custom_extension(self):
        result = build_tagged_filename("Bach", "Suite", set(), ext=".PDF")
        assert result == "Bach - Suite.PDF"


# ---------------------------------------------------------------------------
# rename_score_tags
# ---------------------------------------------------------------------------


class TestRenameScoreTags:
    def test_add_tag(self, tmp_path):
        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.touch()
        score = Score(str(pdf), pdf.name)
        new_score = rename_score_tags(score, {"jazz"})
        assert new_score.filename == "Bach - Suite -- jazz.pdf"
        assert os.path.exists(new_score.filepath)
        assert not os.path.exists(str(pdf))

    def test_remove_tag(self, tmp_path):
        pdf = tmp_path / "Bach - Suite -- jazz.pdf"
        pdf.touch()
        score = Score(str(pdf), pdf.name)
        new_score = rename_score_tags(score, set())
        assert new_score.filename == "Bach - Suite.pdf"
        assert os.path.exists(new_score.filepath)

    def test_sidecar_renamed(self, tmp_path):
        pdf = tmp_path / "Bach - Suite.pdf"
        sidecar = tmp_path / "Bach - Suite.json"
        pdf.touch()
        sidecar.write_text("{}")
        score = Score(str(pdf), pdf.name)
        new_score = rename_score_tags(score, {"jazz"})
        new_sidecar = os.path.splitext(new_score.filepath)[0] + ".json"
        assert os.path.exists(new_sidecar)
        assert not os.path.exists(str(sidecar))

    def test_no_change_returns_same(self, tmp_path):
        pdf = tmp_path / "Bach - Suite -- jazz.pdf"
        pdf.touch()
        score = Score(str(pdf), pdf.name)
        result = rename_score_tags(score, {"jazz"})
        assert result is score

    def test_target_exists_raises(self, tmp_path):
        pdf = tmp_path / "Bach - Suite.pdf"
        target = tmp_path / "Bach - Suite -- jazz.pdf"
        pdf.touch()
        target.touch()
        score = Score(str(pdf), pdf.name)
        with pytest.raises(FileExistsError):
            rename_score_tags(score, {"jazz"})

    def test_folder_tags_preserved(self, tmp_path):
        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.touch()
        score = Score(str(pdf), pdf.name, folder_tags={"classical"})
        new_score = rename_score_tags(score, {"jazz"})
        assert "classical" in new_score.folder_tags
        assert "jazz" in new_score.filename_tags
        assert new_score.tags == {"classical", "jazz"}

    def test_content_hash_preserved(self, tmp_path):
        pdf = tmp_path / "Bach - Suite.pdf"
        pdf.write_bytes(b"%PDF-1.4 content")
        score = Score(str(pdf), pdf.name)
        score.content_hash = compute_content_hash(str(pdf))
        new_score = rename_score_tags(score, {"jazz"})
        assert new_score.content_hash == score.content_hash


# ---------------------------------------------------------------------------
# bake_score
# ---------------------------------------------------------------------------


def _make_real_pdf(path: str) -> None:
    import pymupdf as fitz
    doc = fitz.open()
    doc.new_page(width=400, height=600)
    doc.save(path)
    doc.close()


class TestBakeScore:
    def test_baked_tag_inserted_alphabetically(self, tmp_path):
        pdf = tmp_path / "Sachs - 14 Duets -- trumpet.pdf"
        _make_real_pdf(str(pdf))
        score = Score(str(pdf), pdf.name)

        new_score = bake_score(score)

        assert new_score.filename == "Sachs - 14 Duets -- baked trumpet.pdf"
        assert os.path.exists(new_score.filepath)

    def test_baked_tag_added_to_untagged_score(self, tmp_path):
        pdf = tmp_path / "Bach - Suite.pdf"
        _make_real_pdf(str(pdf))
        score = Score(str(pdf), pdf.name)

        new_score = bake_score(score)

        assert new_score.filename == "Bach - Suite -- baked.pdf"
        assert os.path.exists(new_score.filepath)

    def test_original_file_untouched(self, tmp_path):
        pdf = tmp_path / "Bach - Suite.pdf"
        _make_real_pdf(str(pdf))
        original_bytes = pdf.read_bytes()
        score = Score(str(pdf), pdf.name)

        bake_score(score)

        assert pdf.read_bytes() == original_bytes

    def test_original_sidecar_untouched(self, tmp_path):
        pdf = tmp_path / "Bach - Suite.pdf"
        _make_real_pdf(str(pdf))
        score = Score(str(pdf), pdf.name)

        pages = {"0": [{"uuid": "ink-1", "type": "ink",
                        "points": [[0.1, 0.1], [0.2, 0.2]],
                        "color": "red", "width": 2}]}
        save_annotations(str(pdf), pages, {})
        sidecar = annotation_sidecar_path(str(pdf))
        sidecar_bytes = open(sidecar, "rb").read()

        bake_score(score)

        assert os.path.exists(sidecar)
        assert open(sidecar, "rb").read() == sidecar_bytes

    def test_no_sidecar_created_for_baked_file(self, tmp_path):
        pdf = tmp_path / "Bach - Suite.pdf"
        _make_real_pdf(str(pdf))
        score = Score(str(pdf), pdf.name)

        pages = {"0": [{"uuid": "ink-1", "type": "ink",
                        "points": [[0.1, 0.1], [0.2, 0.2]],
                        "color": "red", "width": 2}]}
        save_annotations(str(pdf), pages, {})

        new_score = bake_score(score)

        assert not os.path.exists(annotation_sidecar_path(new_score.filepath))

    def test_already_baked_raises(self, tmp_path):
        pdf = tmp_path / "Bach - Suite -- baked.pdf"
        _make_real_pdf(str(pdf))
        score = Score(str(pdf), pdf.name)
        assert BAKED_TAG in score.filename_tags

        with pytest.raises(ValueError):
            bake_score(score)

    def test_target_exists_raises(self, tmp_path):
        pdf = tmp_path / "Bach - Suite.pdf"
        _make_real_pdf(str(pdf))
        target = tmp_path / "Bach - Suite -- baked.pdf"
        target.write_bytes(b"%PDF-1.4 squatter")
        score = Score(str(pdf), pdf.name)

        with pytest.raises(FileExistsError):
            bake_score(score)
        # Squatter not overwritten
        assert target.read_bytes() == b"%PDF-1.4 squatter"

    def test_content_hash_set_on_new_score(self, tmp_path):
        pdf = tmp_path / "Bach - Suite.pdf"
        _make_real_pdf(str(pdf))
        score = Score(str(pdf), pdf.name)

        new_score = bake_score(score)

        assert new_score.content_hash
        assert new_score.content_hash == compute_content_hash(new_score.filepath)

    def test_baked_in_same_directory(self, tmp_path):
        subdir = tmp_path / "jazz"
        subdir.mkdir()
        pdf = subdir / "Davis - Blue -- swing.pdf"
        _make_real_pdf(str(pdf))
        score = Score(str(pdf), pdf.name, folder_tags={"jazz"})

        new_score = bake_score(score)

        assert os.path.dirname(new_score.filepath) == str(subdir)
        assert new_score.filename == "Davis - Blue -- baked swing.pdf"
        assert "jazz" in new_score.folder_tags
