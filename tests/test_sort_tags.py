"""scripts/sort_tags.py and the setlist/recent/hash-index helpers it shares
with the server: every stored reference follows a rename, in the current
on-disk formats (paths relative to the library root), and a failure part-way
leaves the library unchanged."""

import json
import os
import runpy
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from web.core import (
    SafeJSON,
    SafeJSONError,
    Score,
    annotation_sidecar_path,
    load_hash_index,
    load_recent,
    load_setlists,
    migrate_to_relative,
    portable_path,
    remap_hash_index,
    rename_score_file,
    remap_recent_paths,
    remap_setlist_paths,
)

SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "sort_tags.py")
find_unsorted_pdfs = runpy.run_path(SCRIPT)["find_unsorted_pdfs"]

OLD = "Bach - Suite -- jazz Blues.pdf"
NEW = "Bach - Suite -- blues jazz.pdf"


def _run(lib: Path, *args: str, monkeypatch) -> int:
    """Run the script in-process (so tests can monkeypatch); return exit code."""
    monkeypatch.setattr(sys, "argv", [SCRIPT, str(lib), *args])
    try:
        runpy.run_path(SCRIPT, run_name="__main__")
    except SystemExit as e:
        return e.code or 0
    return 0


def _read(path: Path):
    return json.loads(path.read_text())


@pytest.fixture
def lib(tmp_path):
    """A library with one unsorted PDF, its sidecar, and every file that
    references it by path, all in the current on-disk formats."""
    pdf = tmp_path / OLD
    pdf.write_bytes(b"%PDF-1.4 fake")
    Path(annotation_sidecar_path(str(pdf))).write_text('{"pages": {}}')
    p = OLD
    (tmp_path / "setlists.json").write_text(json.dumps({
        "Gig": {
            "items": [
                {"type": "song", "path": p, "title": "Suite", "composer": "Bach",
                 "start_page": 1, "end_page": None},
                {"type": "setlist_ref", "setlist_name": "Other"},
            ],
            "shuffle": True,
        },
    }))
    (tmp_path / "_recent.json").write_text(json.dumps([
        {"filepath": p, "composer": "Bach", "title": "Suite", "timestamp": 1},
    ]))
    (tmp_path / "_hash_index.json").write_text(json.dumps({"abc123": p}))
    return tmp_path


def _new_portable(lib: Path) -> str:
    """NEW as stored on disk: relative to the library root."""
    return NEW


# ---------------------------------------------------------------------------
# End-to-end: --apply heals every reference
# ---------------------------------------------------------------------------


def test_apply_heals_current_format_setlists(lib, monkeypatch):
    """Regression: the old script skipped every setlist in the dict format."""
    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 0
    gig = _read(lib / "setlists.json")["Gig"]
    assert gig["items"][0]["path"] == _new_portable(lib)
    # Untouched: the reference item and the setlist's shuffle flag
    assert gig["items"][1] == {"type": "setlist_ref", "setlist_name": "Other"}
    assert gig["shuffle"] is True


def test_apply_heals_recent_list(lib, monkeypatch):
    """Regression: the old script never updated _recent.json."""
    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 0
    assert _read(lib / "_recent.json")[0]["filepath"] == _new_portable(lib)


def test_apply_heals_hash_index_and_moves_sidecar(lib, monkeypatch):
    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 0
    assert _read(lib / "_hash_index.json") == {"abc123": _new_portable(lib)}
    assert (lib / NEW).exists() and not (lib / OLD).exists()
    assert Path(annotation_sidecar_path(str(lib / NEW))).exists()
    assert not Path(annotation_sidecar_path(str(lib / OLD))).exists()


def test_legacy_list_format_setlist_is_healed(lib, monkeypatch):
    p = OLD
    (lib / "setlists.json").write_text(json.dumps({
        "Old": [{"path": p, "title": "Suite", "composer": "Bach",
                 "start_page": 1, "end_page": None}],
    }))
    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 0
    assert _read(lib / "setlists.json")["Old"]["items"][0]["path"] == _new_portable(lib)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_dry_run_changes_nothing(lib, monkeypatch):
    before = {p.name: p.read_bytes() for p in lib.iterdir()}
    assert _run(lib, monkeypatch=monkeypatch) == 0
    assert {p.name: p.read_bytes() for p in lib.iterdir()} == before


def test_sidecar_rename_failure_rolls_back_pdf(lib, monkeypatch):
    """Regression: the old script left the PDF renamed and its sidecar behind."""
    real_rename = os.rename

    def failing_rename(src, dst):
        if str(src).endswith(".json"):
            raise PermissionError("sidecar locked")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", failing_rename)
    before = {p.name: p.read_bytes() for p in lib.iterdir()}
    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 1
    # PDF back under its old name, and no reference moved to the new one
    assert {p.name: p.read_bytes() for p in lib.iterdir()} == before


def test_existing_target_is_skipped_and_exits_nonzero(lib, monkeypatch):
    (lib / NEW).write_bytes(b"%PDF-1.4 someone else")
    before = {p.name: p.read_bytes() for p in lib.iterdir()}
    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 1
    assert {p.name: p.read_bytes() for p in lib.iterdir()} == before


@pytest.mark.parametrize("name", ["setlists.json", "_recent.json", "_hash_index.json"])
def test_corrupt_reference_file_stops_before_any_rename(lib, monkeypatch, name):
    """Regression: a corrupt reference file was only read after the renames,
    leaving files renamed and the other references half-updated."""
    (lib / name).write_text("{not json")
    before = {p.name: p.read_bytes() for p in lib.iterdir()}
    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 1
    assert {p.name: p.read_bytes() for p in lib.iterdir()} == before


def test_malformed_entries_are_skipped_not_fatal(lib, monkeypatch):
    """Regression: a stray non-dict item crashed the reference update after
    the files had already been renamed."""
    sl = _read(lib / "setlists.json")
    sl["Gig"]["items"] += ["junk", None]
    (lib / "setlists.json").write_text(json.dumps(sl))
    (lib / "_recent.json").write_text(json.dumps(_read(lib / "_recent.json") + ["junk"]))
    (lib / "_hash_index.json").write_text(json.dumps(
        {**_read(lib / "_hash_index.json"), "bad": ["not", "a", "path"]}))

    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 0
    gig = _read(lib / "setlists.json")["Gig"]
    assert gig["items"][0]["path"] == _new_portable(lib)
    assert gig["items"][2:] == ["junk", None]
    assert _read(lib / "_recent.json") == [
        {"filepath": _new_portable(lib), "composer": "Bach", "title": "Suite",
         "timestamp": 1},
        "junk",
    ]
    assert _read(lib / "_hash_index.json")["abc123"] == _new_portable(lib)


def test_non_object_hash_index_does_not_stop_other_updates(lib, monkeypatch):
    """Regression: a hash index of the wrong JSON shape loaded fine, then
    crashed the update after the renames, leaving setlists/recent stale."""
    (lib / "_hash_index.json").write_text("[]")
    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 0
    assert _read(lib / "setlists.json")["Gig"]["items"][0]["path"] == _new_portable(lib)
    assert _read(lib / "_recent.json")[0]["filepath"] == _new_portable(lib)
    assert _read(lib / "_hash_index.json") == []


def test_failed_save_reports_and_still_saves_the_others(lib, monkeypatch):
    """Regression: one unsavable reference file stopped the rest being saved."""
    real_save = SafeJSON.save

    def save(path, data):
        if path.endswith("_hash_index.json"):
            raise SafeJSONError("read-only")
        real_save(path, data)

    monkeypatch.setattr(SafeJSON, "save", staticmethod(save))
    old_index = _read(lib / "_hash_index.json")
    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 1
    assert (lib / NEW).exists()
    assert _read(lib / "_hash_index.json") == old_index
    assert _read(lib / "setlists.json")["Gig"]["items"][0]["path"] == _new_portable(lib)
    assert _read(lib / "_recent.json")[0]["filepath"] == _new_portable(lib)


# ---------------------------------------------------------------------------
# Library-relative storage
# ---------------------------------------------------------------------------


def test_heals_files_written_under_another_mount_root(tmp_path, monkeypatch):
    """Regression: Folio (in Docker) stored paths under the container's root,
    so the script run on the host path matched none of them and updated
    nothing. Now the server stores them relative, so any root works."""
    container = tmp_path / "container" / "Music"
    container.mkdir(parents=True)
    (container / OLD).write_bytes(b"%PDF-1.4 fake")
    p = portable_path(str(container / OLD))
    (container / "setlists.json").write_text(json.dumps(
        {"Gig": {"items": [{"type": "song", "path": p}], "shuffle": False}}))
    (container / "_recent.json").write_text(json.dumps([{"filepath": p}]))
    (container / "_hash_index.json").write_text(json.dumps({"abc123": p}))
    migrate_to_relative(str(container))  # what the server does on open

    host = tmp_path / "host" / "Music"
    host.parent.mkdir()
    os.rename(container, host)
    assert _run(host, "--apply", monkeypatch=monkeypatch) == 0
    assert _read(host / "setlists.json")["Gig"]["items"][0]["path"] == NEW
    assert _read(host / "_recent.json")[0]["filepath"] == NEW
    assert _read(host / "_hash_index.json") == {"abc123": NEW}


def test_relative_library_argument(lib, monkeypatch):
    monkeypatch.chdir(lib)
    assert _run(Path("."), "--apply", monkeypatch=monkeypatch) == 0
    assert (lib / NEW).exists()
    assert _read(lib / "setlists.json")["Gig"]["items"][0]["path"] == NEW


def test_legacy_absolute_paths_inside_library_are_healed(lib, monkeypatch):
    """Files not yet converted by the server, but written under this same
    root, still heal -- and are saved in relative form."""
    p = portable_path(str(lib / OLD))
    (lib / "setlists.json").write_text(json.dumps(
        {"Gig": {"items": [{"type": "song", "path": p}], "shuffle": False}}))
    (lib / "_recent.json").write_text(json.dumps([{"filepath": p}]))
    (lib / "_hash_index.json").write_text(json.dumps({"abc123": p}))
    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 0
    assert _read(lib / "setlists.json")["Gig"]["items"][0]["path"] == NEW
    assert _read(lib / "_recent.json")[0]["filepath"] == NEW
    assert _read(lib / "_hash_index.json") == {"abc123": NEW}


def test_unconverted_files_from_another_root_stop_before_any_rename(
        lib, monkeypatch, capsys):
    """Absolute paths under a different root, with nothing yet converted:
    renaming now would leave every one of them pointing at the old name."""
    p = "/elsewhere/Music/" + OLD
    (lib / "setlists.json").write_text(json.dumps(
        {"Gig": {"items": [{"type": "song", "path": p}], "shuffle": False}}))
    (lib / "_recent.json").write_text(json.dumps([{"filepath": p}]))
    (lib / "_hash_index.json").write_text(json.dumps({"abc123": p}))
    before = {q.name: q.read_bytes() for q in lib.iterdir()}
    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 1
    assert {q.name: q.read_bytes() for q in lib.iterdir()} == before
    err = capsys.readouterr().err
    assert "Open this library in Folio once" in err
    assert "No files were renamed" in err


def test_outside_paths_in_converted_files_do_not_block_renames(
        lib, monkeypatch, capsys):
    """Regression: once converted, a genuine reference outside the library
    (which Folio allows) made the script refuse every future run."""
    outside = "/elsewhere/Other.pdf"
    sl = _read(lib / "setlists.json")
    sl["Gig"]["items"].append({"type": "song", "path": outside})
    (lib / "setlists.json").write_text(json.dumps(sl))

    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 0
    assert (lib / NEW).exists()
    items = _read(lib / "setlists.json")["Gig"]["items"]
    assert items[0]["path"] == NEW
    assert items[2] == {"type": "song", "path": outside}
    assert "1 stored path(s) point outside the library" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Case-only renames (case-insensitive filesystems: Windows, macOS, WSL /mnt)
# ---------------------------------------------------------------------------

CASE_OLD = "Bach - Suite -- Blues jazz.pdf"
CASE_NEW = "Bach - Suite -- blues jazz.pdf"


def test_case_only_rename_on_simulated_case_insensitive_fs(tmp_path, monkeypatch):
    """Regression: the "target exists" check found the source file itself, so
    every lowercase-only rename was skipped on a case-insensitive drive."""
    real_exists = os.path.exists

    def ci_exists(p):
        d, n = os.path.split(str(p))
        return real_exists(p) or (os.path.isdir(d) and any(
            x.lower() == n.lower() for x in os.listdir(d)))

    monkeypatch.setattr(os.path, "exists", ci_exists)
    monkeypatch.setattr(os.path, "samefile",
                        lambda a, b: str(a).lower() == str(b).lower())
    (tmp_path / CASE_OLD).write_bytes(b"%PDF-1.4 fake")
    assert _run(tmp_path, "--apply", monkeypatch=monkeypatch) == 0
    assert sorted(os.listdir(tmp_path)) == [CASE_NEW]


def test_hard_link_target_is_refused(lib, monkeypatch):
    """Regression: samefile() let a hard-linked target through; os.rename then
    did nothing, yet the sidecar and every reference moved to the new name."""
    os.link(lib / OLD, lib / NEW)
    before = {p.name: p.read_bytes() for p in lib.iterdir()}
    assert _run(lib, "--apply", monkeypatch=monkeypatch) == 1
    assert {p.name: p.read_bytes() for p in lib.iterdir()} == before


def test_rename_score_file_refuses_hard_link(tmp_path):
    src = tmp_path / "A -- b a.pdf"
    src.write_bytes(b"x")
    os.link(src, tmp_path / "A -- a b.pdf")
    with pytest.raises(FileExistsError):
        rename_score_file(Score(str(src), src.name), "A -- a b.pdf")


def _require_case_sensitive(d: Path) -> None:
    probe = d / "Probe"
    probe.write_bytes(b"")
    insensitive = (d / "probe").exists()
    probe.unlink()
    if insensitive:
        pytest.skip("tmp_path is on a case-insensitive filesystem")


def test_case_only_hard_link_is_refused_on_case_sensitive_fs(tmp_path, monkeypatch):
    """Regression: on a case-sensitive drive (e.g. inside the container) a
    hard link whose name differs only in case passed the case-only
    exception; os.rename did nothing, yet the sidecar and every reference
    moved to the new name."""
    _require_case_sensitive(tmp_path)
    src = tmp_path / CASE_OLD
    src.write_bytes(b"%PDF-1.4 fake")
    Path(annotation_sidecar_path(str(src))).write_text("{}")
    os.link(src, tmp_path / CASE_NEW)
    with pytest.raises(FileExistsError):
        rename_score_file(Score(str(src), src.name), CASE_NEW)

    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert _run(tmp_path, "--apply", monkeypatch=monkeypatch) == 1
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_case_only_rename_on_real_case_sensitive_fs(tmp_path, monkeypatch):
    """A plain case-only rename where the new name doesn't exist yet. The
    simulated test above fakes exists(), so without this nothing checks the
    ordinary Linux/container path (a check reordered to call samefile() on
    the missing target crashed here)."""
    _require_case_sensitive(tmp_path)
    (tmp_path / CASE_OLD).write_bytes(b"%PDF-1.4 fake")
    Path(annotation_sidecar_path(str(tmp_path / CASE_OLD))).write_text("{}")
    assert _run(tmp_path, "--apply", monkeypatch=monkeypatch) == 0
    assert sorted(os.listdir(tmp_path)) == sorted(
        [CASE_NEW, Path(annotation_sidecar_path(str(tmp_path / CASE_NEW))).name])


def _case_insensitive_dir():
    """A scratch dir on the repo's own drive if that drive ignores case
    (e.g. the /mnt/z checkout), else None."""
    d = tempfile.mkdtemp(prefix=".casetest-", dir=Path(__file__).resolve().parent.parent)
    probe = Path(d) / "Probe"
    probe.write_bytes(b"")
    if (Path(d) / "probe").exists():
        probe.unlink()
        return Path(d)
    shutil.rmtree(d)
    return None


def test_case_only_rename_on_real_case_insensitive_fs(monkeypatch):
    d = _case_insensitive_dir()
    if d is None:
        pytest.skip("repo is not on a case-insensitive filesystem")
    try:
        (d / CASE_OLD).write_bytes(b"%PDF-1.4 fake")
        Path(annotation_sidecar_path(str(d / CASE_OLD))).write_text("{}")
        assert _run(d, "--apply", monkeypatch=monkeypatch) == 0
        assert sorted(os.listdir(d)) == sorted(
            [CASE_NEW, Path(annotation_sidecar_path(str(d / CASE_NEW))).name])
    finally:
        shutil.rmtree(d)


def test_missing_reference_files_are_fine(tmp_path, monkeypatch):
    (tmp_path / OLD).write_bytes(b"%PDF-1.4 fake")
    assert _run(tmp_path, "--apply", monkeypatch=monkeypatch) == 0
    assert (tmp_path / NEW).exists()
    assert not (tmp_path / "setlists.json").exists()
    assert not (tmp_path / "_recent.json").exists()


# ---------------------------------------------------------------------------
# Which filenames count as unsorted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fname, expected", [
    ("Bach - Suite -- a b.pdf", None),                   # already canonical
    ("Bach - Suite.pdf", None),                          # no tags
    ("Bach - Suite -- b a.pdf", "Bach - Suite -- a b.pdf"),
    ("Bach - Suite -- A b.pdf", "Bach - Suite -- a b.pdf"),  # lowercased
    ("Bach - Suite -- b a b.pdf", "Bach - Suite -- a b.pdf"),  # de-duplicated
    ("Suite -- b a.pdf", "Suite -- a b.pdf"),            # no composer
    ("Bach - Suite -- b a.PDF", "Bach - Suite -- a b.PDF"),  # extension kept
    ("notes -- b a.txt", None),                          # not a PDF
])
def test_find_unsorted(tmp_path, fname, expected):
    (tmp_path / fname).write_bytes(b"x")
    found = find_unsorted_pdfs(str(tmp_path))
    assert [new for _score, new in found] == ([expected] if expected else [])


# ---------------------------------------------------------------------------
# Shared helpers in web.core
# ---------------------------------------------------------------------------


def test_remap_setlist_paths_counts_and_skips_refs():
    data = {"A": {"items": [
        {"type": "song", "path": "x"},
        {"path": "x"},                                   # legacy: no type = song
        {"type": "setlist_ref", "setlist_name": "x", "path": "x"},
        {"type": "song", "path": "y"},
        "junk", None, 3,                                 # malformed: skipped
    ], "shuffle": False}}
    assert remap_setlist_paths(data, {"x": "z"}) == 2
    assert data["A"]["items"] == [
        {"type": "song", "path": "z"}, {"path": "z"},
        {"type": "setlist_ref", "setlist_name": "x", "path": "x"},
        {"type": "song", "path": "y"}, "junk", None, 3,
    ]


def test_remap_hash_index_counts():
    index = {"h1": "x", "h2": "y", "h3": None, "h4": ["x"]}   # unhashable too
    assert remap_hash_index(index, {"x": "z"}) == 1
    assert index == {"h1": "z", "h2": "y", "h3": None, "h4": ["x"]}


def test_remap_recent_paths_counts():
    data = [{"filepath": "x"}, {"filepath": "y"}, {}, "junk", None]
    assert remap_recent_paths(data, {"x": "z"}) == 1
    assert data == [{"filepath": "z"}, {"filepath": "y"}, {}, "junk", None]


def test_loaders_default_when_missing(tmp_path):
    root = str(tmp_path)
    assert load_setlists(str(tmp_path / "setlists.json"), root) == {}
    assert load_recent(str(tmp_path / "_recent.json"), root) == []
    assert load_hash_index(str(tmp_path / "_hash_index.json"), root) == {}


@pytest.mark.parametrize("loader", [load_setlists, load_recent, load_hash_index])
def test_loaders_raise_on_corrupt(tmp_path, loader):
    path = tmp_path / "f.json"
    path.write_text("{oops")
    with pytest.raises(SafeJSONError):
        loader(str(path), str(tmp_path))


def test_loaders_ignore_wrong_top_level_type(tmp_path):
    path = tmp_path / "f.json"
    root = str(tmp_path)
    path.write_text("[1, 2]")
    assert load_setlists(str(path), root) == {}
    assert load_hash_index(str(path), root) == {}
    path.write_text('{"a": 1}')
    assert load_recent(str(path), root) == []
