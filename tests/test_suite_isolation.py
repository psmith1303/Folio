"""The suite never reads or writes the real ~/.folio. web.server reads (and,
for obsolete keys, rewrites) web_config.json at import, and saves config,
setlists and recents at run time; conftest.py points all of it at scratch.
conftest's real_folio_untouched also fails the run if the real folder changes."""
import os
import shutil
import subprocess
import sys

import pytest

import web.server as srv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REAL_HOME = os.path.realpath(os.environ["FOLIO_TEST_REAL_HOME"])


def _real(name):
    return os.path.join(REAL_HOME, name)


def _outside(path, real_dir):
    path = os.path.realpath(path)
    return os.path.commonpath([path, real_dir]) != real_dir


def test_home_is_not_the_real_one():
    assert os.path.realpath(os.path.expanduser("~")) != REAL_HOME


def test_web_server_was_imported_with_a_scratch_home():
    # Computed from HOME at import, and never patched: shows where the
    # import-time config load and the old-folder rename looked.
    assert _outside(srv._OLD_CONFIG_DIR, _real(".music_score_viewer"))


def test_run_time_config_files_are_per_test_scratch():
    for path in (srv.WEB_CONFIG_PATH, srv.CONFIG_DIR,
                 srv.state.setlist_path(), srv.state.recent_path()):
        assert _outside(path, _real(".folio")), path
    # conftest's per-test folder, not just the scratch HOME's ~/.folio.
    assert os.path.basename(os.path.dirname(srv.WEB_CONFIG_PATH)).startswith("folio-config")


def test_library_dir_set_directly_by_one_test():
    # web/__main__.py assigns LIBRARY_DIR directly, as this does.
    srv.LIBRARY_DIR = "/somewhere/else"


def test_library_dir_is_reset_for_the_next_test():
    assert srv.LIBRARY_DIR == ""


# The checks below run a small suite under this conftest in a subprocess, with
# a stand-in for the real home: never the real one.

def _run_suite(tmp_path, body, home, env_real_home=None):
    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copy(os.path.join(REPO, "tests", "conftest.py"), suite)
    (suite / "test_inner.py").write_text("import os\n\n\ndef test_inner():\n" + body)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "FOLIO_"))}
    env.update(HOME=str(home), PYTHONPATH=REPO)
    if env_real_home:
        env["FOLIO_TEST_REAL_HOME"] = env_real_home
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"--basetemp={tmp_path / 'basetemp'}", str(suite)],
        cwd=suite, env=env, capture_output=True, text=True, timeout=120)


@pytest.fixture
def stand_in_home(tmp_path):
    home = tmp_path / "home"
    (home / ".folio").mkdir(parents=True)
    (home / ".folio" / "web_config.json").write_text('{"keybindings": {}}')
    return home


@pytest.mark.parametrize("writes", [True, False])
def test_run_fails_when_a_test_changes_the_real_folio(tmp_path, stand_in_home, writes):
    target = stand_in_home / ".folio" / "web_config.json"
    body = (f"    with open({str(target)!r}, 'w') as f:\n        f.write('{{}}')\n"
            if writes else "    pass\n")
    r = _run_suite(tmp_path, body, stand_in_home)
    out = r.stdout + r.stderr
    if writes:
        assert r.returncode != 0, out
        assert "1 passed, 1 error" in out, out
        assert "the suite changed the real ~/.folio" in out, out
    else:
        assert r.returncode == 0, out


def test_leaked_real_home_variable_does_not_stop_the_swap(tmp_path, stand_in_home):
    # A nested run inherits FOLIO_TEST_REAL_HOME; HOME still decides.
    body = (f"    assert os.environ['FOLIO_TEST_REAL_HOME'] == {str(stand_in_home)!r}\n"
            f"    assert os.path.expanduser('~') != {str(stand_in_home)!r}\n")
    r = _run_suite(tmp_path, body, stand_in_home, env_real_home=str(tmp_path / "leaked"))
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_scratch_home_is_kept(tmp_path, stand_in_home):
    scratch = tmp_path / "folio-test-home-outer"
    scratch.mkdir()
    body = (f"    assert os.path.expanduser('~') == {str(scratch)!r}\n"
            f"    assert os.environ['FOLIO_TEST_REAL_HOME'] == {str(stand_in_home)!r}\n")
    r = _run_suite(tmp_path, body, scratch, env_real_home=str(stand_in_home))
    assert r.returncode == 0, r.stdout + r.stderr
