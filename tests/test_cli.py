"""web/__main__.py: `python -m web --library DIR` fixes the library at
start-up (the browser can no longer choose it). The start-up scan of
LIBRARY_DIR itself is tested in test_server_efficiency.py."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

import web.__main__ as cli
import web.server as srv
from web.core import normalize_path


def test_library_is_required(capsys):
    with pytest.raises(SystemExit) as e:
        cli.parse_args([])
    assert e.value.code == 2
    assert "--library" in capsys.readouterr().err


def test_library_must_be_a_directory(tmp_path, capsys):
    f = tmp_path / "file.txt"
    f.write_text("x")
    for bad in (tmp_path / "missing", f):
        with pytest.raises(SystemExit):
            cli.parse_args(["--library", str(bad)])
        assert "not a directory" in capsys.readouterr().err


def test_main_sets_the_library_and_runs_the_app(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setattr(srv, "LIBRARY_DIR", "")
    cli.main(["--library", str(tmp_path), "--port", "9000"])
    assert srv.LIBRARY_DIR == normalize_path(str(tmp_path))
    assert calls == [(srv.app, {"host": "0.0.0.0", "port": 9000})]


def test_a_relative_library_is_made_absolute(tmp_path, monkeypatch):
    (tmp_path / "Music").mkdir()
    monkeypatch.chdir(tmp_path)
    assert cli.parse_args(["--library", "Music"]).library == str(tmp_path / "Music")


# ---------------------------------------------------------------------------
# The real process. The image's test stage never runs the runtime CMD
# (python -m web --library /library), so a start-up that only works when
# monkeypatched would pass the push gate and crash-loop the container.
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_web(args, home, **kw):
    # HOME is where the server keeps ~/.folio/web_config.json: never the real one.
    env = {**os.environ, "HOME": str(home)}
    return subprocess.Popen([sys.executable, "-m", "web", *args], cwd=REPO, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)


def test_python_m_web_serves_the_library_it_was_given(tmp_path):
    lib = tmp_path / "library"
    (lib / "jazz").mkdir(parents=True)
    (lib / "Bach - Suite.pdf").write_bytes(b"%PDF-1.4 cli one")
    (lib / "jazz" / "Davis - Blue.pdf").write_bytes(b"%PDF-1.4 cli two")
    home = tmp_path / "home"
    home.mkdir()
    port = _free_port()
    proc = _run_web(["--library", str(lib), "--host", "127.0.0.1", "--port", str(port)], home)
    try:
        deadline = time.monotonic() + 30
        while True:
            assert proc.poll() is None, f"server exited:\n{proc.communicate()[0]}"
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=2) as r:
                    cfg = json.load(r)
                break
            except OSError:
                assert time.monotonic() < deadline, "server never answered"
                time.sleep(0.2)
        assert cfg["score_count"] == 2
        assert os.path.samefile(cfg["library_dir"], lib)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_python_m_web_without_a_library_exits_with_usage(tmp_path):
    proc = _run_web([], tmp_path)
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 2
    assert "usage: python -m web" in out and "--library" in out
