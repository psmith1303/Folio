"""Server efficiency and write safety (cleanup Step 4).

- SafeJSON.save writes a temp file beside the target and renames it into
  place (atomic), and skips the write when nothing changed.
- Annotation load/save read the sidecar once.
- Importing web.server doesn't scan the library; the lifespan does.
- Score lookups by path go through an index that follows renames.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import web.core as core
import web.server as srv
from web.core import (
    SafeJSON,
    SafeJSONError,
    annotations_etag,
    load_annotations,
    save_annotations,
)
from web.server import app, state

REPO = Path(__file__).resolve().parent.parent


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
def replaces(monkeypatch):
    """Record every os.replace(src, dst) and fail the test on a copy fallback."""
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def recording_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    def no_copy(*args, **kwargs):
        raise AssertionError("SafeJSON.save fell back to a non-atomic copy")

    monkeypatch.setattr(os, "replace", recording_replace)
    monkeypatch.setattr(shutil, "copyfile", no_copy)
    return calls


@pytest.fixture
def wd(tmp_path):
    """An empty directory to save into (tmp_path also holds the config dir)."""
    d = tmp_path / "work"
    d.mkdir()
    return d


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


# ---------------------------------------------------------------------------
# SafeJSON.save
# ---------------------------------------------------------------------------


def test_save_renames_a_temp_file_from_the_target_directory(wd, replaces):
    """Regression: the temp file was made in /tmp, a different filesystem from
    /mnt drives and Docker bind mounts, so every save became a non-atomic
    copy over the target."""
    target = wd / "setlists.json"
    target.write_text('{"old": 1}')
    SafeJSON.save(str(target), {"new": 1})

    assert len(replaces) == 1
    src, dst = replaces[0]
    assert dst == str(target)
    assert os.path.dirname(src) == str(wd)
    assert json.loads(target.read_text()) == {"new": 1}
    assert os.listdir(wd) == ["setlists.json"]  # no temp left behind


def test_save_on_the_repo_drive_is_atomic(replaces):
    """The real-mount case: when the repo's drive is not the temp dir's
    filesystem (e.g. the /mnt/z checkout), a save must still rename, not copy."""
    d = Path(tempfile.mkdtemp(prefix=".savetest-", dir=REPO))
    try:
        if os.stat(d).st_dev == os.stat(tempfile.gettempdir()).st_dev:
            pytest.skip("repo shares a filesystem with the temp dir")
        SafeJSON.save(str(d / "f.json"), {"v": 1})
        SafeJSON.save(str(d / "f.json"), {"v": 2})
        assert json.loads((d / "f.json").read_text()) == {"v": 2}
        assert len(replaces) == 2
    finally:
        shutil.rmtree(d)


def test_saved_file_gets_normal_permissions(tmp_path):
    """Regression: a mkstemp temp file is 0600, and os.replace keeps that."""
    target = tmp_path / "f.json"
    SafeJSON.save(str(target), {"v": 1})
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o666 & ~_umask()


@pytest.mark.parametrize("mode", [0o660, 0o664, 0o600, 0o640], ids=oct)
def test_save_keeps_the_existing_files_permissions(wd, mode):
    """Regression: the atomic rename replaces the file with a new one, which
    got the default mode, so e.g. a group-writable setlists.json stopped
    being writable by the group after the first save."""
    target = wd / "setlists.json"
    target.write_text('{"old": 1}')
    os.chmod(target, mode)
    SafeJSON.save(str(target), {"new": 1})
    SafeJSON.save(str(target), {"new": 2})
    assert stat.S_IMODE(os.stat(target).st_mode) == mode
    assert json.loads(target.read_text()) == {"new": 2}
    assert os.listdir(wd) == ["setlists.json"]


def test_unchanged_save_does_not_write(tmp_path, replaces):
    """Regression: unchanged saves (e.g. the hash index and scan cache on every
    library open) rewrote the file, bumping its mtime and waking file sync."""
    target = tmp_path / "f.json"
    SafeJSON.save(str(target), {"v": 1})
    before = os.stat(target).st_mtime_ns
    time.sleep(0.01)

    assert SafeJSON.save(str(target), {"v": 1}) == target.read_bytes()
    assert os.stat(target).st_mtime_ns == before
    assert len(replaces) == 1

    SafeJSON.save(str(target), {"v": 2})
    assert json.loads(target.read_text()) == {"v": 2}
    assert len(replaces) == 2


def test_save_returns_the_bytes_written(tmp_path):
    target = tmp_path / "f.json"
    assert SafeJSON.save(str(target), {"v": 1}) == target.read_bytes()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_unreadable_target_is_overwritten(wd):
    """The unchanged check must not stop a save from repairing a file that
    exists but can't be read."""
    target = wd / "web_config.json"
    target.write_text('{"old": 1}')
    target.chmod(0o200)
    try:
        SafeJSON.save(str(target), {"new": 1})
    finally:
        target.chmod(0o644)
    assert json.loads(target.read_text()) == {"new": 1}
    assert os.listdir(wd) == ["web_config.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows falls back to a copy")
def test_failed_rename_raises_and_leaves_the_old_file(wd, monkeypatch):
    """A rename that fails is reported, not papered over with a copy; the
    original is untouched and no temp file is left behind."""
    target = wd / "f.json"
    target.write_text('{"old": 1}')

    def failing_replace(src, dst):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(SafeJSONError):
        SafeJSON.save(str(target), {"new": 1})
    assert target.read_text() == '{"old": 1}'
    assert os.listdir(wd) == ["f.json"]


def test_reopening_a_library_does_not_rewrite_its_index_files(tmp_path):
    """End to end: a second open with nothing changed leaves the hash index
    and scan cache files alone."""
    lib = tmp_path / "Music"
    lib.mkdir()
    (lib / "Bach - Suite.pdf").write_bytes(b"%PDF-1.4 unique bach")
    state.set_library(str(lib))
    files = [lib / "_hash_index.json", lib / "_scan_cache.json"]
    before = [os.stat(f).st_mtime_ns for f in files]
    time.sleep(0.01)
    state.set_library(str(lib))
    assert [os.stat(f).st_mtime_ns for f in files] == before


# ---------------------------------------------------------------------------
# Annotations: one sidecar read per load or save
# ---------------------------------------------------------------------------


@pytest.fixture
def sidecar_reads(monkeypatch):
    """Count read-mode opens of any annotation sidecar (*.json next to x.pdf)."""
    import builtins

    reads: list[str] = []
    real_open = builtins.open

    def counting_open(file, mode="r", *args, **kwargs):
        if str(file).endswith("x.json") and "r" in mode:
            reads.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    return reads


def _pdf(tmp_path: Path) -> str:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    return str(pdf)


INK = {"type": "ink", "uuid": "u1", "points": [[0, 0], [1, 1]]}


def test_load_reads_the_sidecar_once(tmp_path, sidecar_reads):
    """Regression: load read the sidecar once for its content and again for
    its etag."""
    pdf = _pdf(tmp_path)
    etag = save_annotations(pdf, {"0": [INK]}, {})
    sidecar_reads.clear()
    data = load_annotations(pdf)
    assert len(sidecar_reads) == 1
    assert data["etag"] == etag
    assert data["pages"] == {"0": [INK]}


def test_save_with_etag_reads_the_sidecar_once(tmp_path, sidecar_reads):
    """Regression: a save read the sidecar for the conflict check and again
    afterwards to compute the new etag."""
    pdf = _pdf(tmp_path)
    etag = save_annotations(pdf, {"0": [INK]}, {})
    sidecar_reads.clear()
    new_etag = save_annotations(pdf, {"0": []}, {}, expected_etag=etag)
    assert len(sidecar_reads) == 1
    assert new_etag == annotations_etag(pdf) != etag


def test_stale_etag_still_conflicts(tmp_path):
    pdf = _pdf(tmp_path)
    etag = save_annotations(pdf, {"0": [INK]}, {})
    save_annotations(pdf, {"0": []}, {}, expected_etag=etag)
    with pytest.raises(core.AnnotationConflictError):
        save_annotations(pdf, {"0": [INK]}, {}, expected_etag=etag)


@pytest.mark.parametrize("content, pages", [
    (None, {}),                                   # no sidecar
    ("{bad json", {}),                            # corrupt
    ("", {}),                                     # empty file
    ('{"version": 2, "rotations": {}, "pages": {"0": [{"type": "ink", "uuid": "u"}]}}',
     {"0": [{"type": "ink", "uuid": "u"}]}),
])
def test_load_etag_matches_the_file(tmp_path, content, pages):
    """The etag load returns is the one a later conflict check computes."""
    pdf = _pdf(tmp_path)
    if content is not None:
        (tmp_path / "x.json").write_text(content)
    data = load_annotations(pdf)
    assert data["pages"] == pages
    assert data["etag"] == annotations_etag(pdf)
    assert (data["etag"] == "") == (content is None)


def test_legacy_sidecar_is_migrated_and_etag_follows(tmp_path):
    pdf = _pdf(tmp_path)
    (tmp_path / "x.json").write_text('{"0": [{"type": "ink"}]}')
    data = load_annotations(pdf)
    assert data["pages"]["0"][0]["uuid"]
    assert data["etag"] == annotations_etag(pdf)
    assert json.loads((tmp_path / "x.json").read_text())["version"] == core.ANNOTATION_VERSION


# ---------------------------------------------------------------------------
# Startup: the library is scanned in the lifespan, not at import
# ---------------------------------------------------------------------------


def test_import_does_not_scan_the_library_but_startup_does(tmp_path):
    """Regression: importing web.server (e.g. from any test) scanned the
    configured library at import time."""
    home = tmp_path / "home"
    (home / ".folio").mkdir(parents=True)
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ["Bach - A.pdf", "Mozart - B.pdf"]:
        (lib / name).write_bytes(b"%PDF-1.4 " + name.encode())
    (home / ".folio" / "web_config.json").write_text(
        json.dumps({"last_directory": str(lib)}))
    script = (
        "import json, web.server as s\n"
        "from fastapi.testclient import TestClient\n"
        "at_import = len(s.state.scores)\n"
        "with TestClient(s.app) as c:\n"
        "    at_startup = c.get('/api/config').json()['score_count']\n"
        "print(json.dumps([at_import, at_startup]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True,
        env={**os.environ, "HOME": str(home)}, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == [0, 2]


# ---------------------------------------------------------------------------
# Score index
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "Music"
    root.mkdir()
    (root / "Bach - Suite -- cello.pdf").write_bytes(b"%PDF-1.4 bach")
    (root / "Mozart - Sonata.pdf").write_bytes(b"%PDF-1.4 mozart")
    state.set_library(str(root))
    return root


def test_lookups_follow_a_rename(client, lib):
    old = str(lib / "Bach - Suite -- cello.pdf")
    resp = client.put("/api/scores/tags", json={"path": old, "filename_tags": ["baroque"]})
    assert resp.status_code == 200
    new = resp.json()["score"]["filepath"]

    assert client.get("/api/scores", params={"path": new}).status_code == 200
    assert client.get("/api/scores", params={"path": old}).status_code == 404
    assert client.post("/api/recent", json={"path": new}).status_code == 200
    assert client.post("/api/recent", json={"path": old}).status_code == 404
    [entry] = client.get("/api/recent").json()["recent"]
    assert entry["filepath"] == new and entry["tags"] == ["baroque"]
    # The other score is unaffected
    other = str(lib / "Mozart - Sonata.pdf")
    assert client.get("/api/scores", params={"path": other}).status_code == 200


def test_recent_tags_come_from_the_index(client, lib):
    path = srv.portable_path(str(lib / "Bach - Suite -- cello.pdf"))
    client.post("/api/recent", json={"path": path})
    srv._save_recent(srv._load_recent() + [{"filepath": "/gone.pdf"}, {"x": 1}])
    recent = client.get("/api/recent").json()["recent"]
    assert [e["tags"] for e in recent] == [["cello"], [], []]


def test_assigning_scores_rebuilds_the_index(lib):
    path = str(lib / "Mozart - Sonata.pdf")
    assert state.find_score(path) is not None
    state.scores = []
    assert state.find_score(path) is None
    state.set_library(str(lib))
    assert state.find_score(path).title == "Sonata"
