"""
Pytest configuration for Folio tests.

Adds the project root to sys.path so that the `web` package is importable
from within the tests/ directory without an installed package.

Keeps the suite away from the real ~/.folio: web.server reads (and can
rewrite) ~/.folio/web_config.json when it is imported, so HOME is a scratch
folder before anything imports it, and every test gets its own config folder.
"""
import atexit
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# FOLIO_TEST_REAL_HOME keeps the real HOME for the checks below. A HOME that
# is already a scratch one (this file imported twice, a nested run) is kept.
_SCRATCH_PREFIX = "folio-test-home-"
if not os.path.basename(os.path.expanduser("~")).startswith(_SCRATCH_PREFIX):
    os.environ["FOLIO_TEST_REAL_HOME"] = os.path.expanduser("~")
    _home = tempfile.mkdtemp(prefix=_SCRATCH_PREFIX)
    atexit.register(shutil.rmtree, _home, ignore_errors=True)
    os.environ["HOME"] = _home

import web.server as srv  # noqa: E402  (after the HOME swap, on purpose)


@pytest.fixture(autouse=True)
def reset_server(tmp_path_factory, monkeypatch):
    """Reset server state and give each test its own config folder (outside
    its tmp_path, so tests that list tmp_path don't see it)."""
    root = tmp_path_factory.mktemp("folio-config")
    monkeypatch.setattr(srv, "WEB_CONFIG_PATH", str(root / "web_config.json"))
    config_dir = root / "config"
    config_dir.mkdir()
    monkeypatch.setattr(srv, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(srv, "LIBRARY_DIR", "")  # web/__main__.py sets it directly
    srv.state.library_dir = ""
    srv.state.scores = []
    srv.state.config = {"keybindings": {}}
    srv._rate_buckets.clear()


def _real_folio_files():
    """(name, size, mtime) of each file in the real ~/.folio; empty if none."""
    real = os.path.join(os.environ["FOLIO_TEST_REAL_HOME"], ".folio")
    try:
        entries = list(os.scandir(real))
    except FileNotFoundError:
        return set()
    return {(e.name, e.stat().st_size, e.stat().st_mtime_ns) for e in entries if e.is_file()}


@pytest.fixture(scope="session", autouse=True)
def real_folio_untouched():
    """Fail the run if anything in it changed a file in the real ~/.folio."""
    before = _real_folio_files()
    yield
    assert _real_folio_files() == before, "the suite changed the real ~/.folio"
