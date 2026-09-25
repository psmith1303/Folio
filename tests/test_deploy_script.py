"""scripts/deploy.sh: deploy Folio to its Docker host.

Runs the real script from a throwaway git repo holding just the build
inputs, against a second directory standing in for the host's Syncthing
copy. `ssh` runs its command locally; `docker` and `curl` are fakes that
record what they were asked to do and answer as the host would.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy.sh"
VERSION = "9.9.9"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or shutil.which("git") is None,
                                reason="needs bash and git")

FAKE_SSH = r"""#!/bin/bash
# Drop -o options and the host; run the command here, as the host would.
while [[ "$1" == -o ]]; do shift 2; done
[[ -n "$FAKE_SSH_FAIL" ]] && { echo "ssh: connect to host $1: No route to host" >&2; exit 255; }
shift
exec bash -c "$*"
"""

FAKE_DOCKER = r"""#!/bin/bash
echo "docker $* | CADDY_ENV_FILE=${CADDY_ENV_FILE:-} | cwd=$PWD" >> "$FAKE/docker.log"
[[ "$*" == *"up -d --build folio"* ]] && touch "$FAKE/deployed"
[[ "$*" == "exec folio id -u" ]] && echo "${FAKE_UID:-1000}"
exit 0
"""

FAKE_CURL = r"""#!/bin/bash
case "$*" in
  *folio.test*) [[ -n "$FAKE_PUBLIC_DOWN" ]] && exit 7
                echo "{\"info\":{\"version\":\"$FAKE_NEW\"}}" ;;
  *openapi.json*) if [[ -f "$FAKE/deployed" ]]; then v=$FAKE_NEW; else v=1.0.0; fi
                  echo "{\"info\":{\"version\":\"$v\"}}" ;;
  *api/config*) [[ -n "$FAKE_CONFIG_FAIL" ]] && exit 22
                echo "{\"score_count\": ${FAKE_SCORES:-433}}" ;;
  *) exit 22 ;;
esac
"""


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def env(tmp_path):
    """A repo with the build inputs and a committed deploy.sh, an in-sync
    host copy of it, and the fakes on PATH."""
    repo = tmp_path / "Folio"
    (repo / "scripts").mkdir(parents=True)
    (repo / "web" / "static").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "deploy.sh")
    (repo / "web" / "static" / "sw.js").write_text(f'const APP_VERSION = "{VERSION}";\n')
    (repo / "web" / "server.py").write_text(f'app = FastAPI(\n    title="Folio", version="{VERSION}",\n)\n')
    (repo / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (repo / ".dockerignore").write_text("tests/\n")
    (repo / "requirements.txt").write_text("fastapi\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")

    host = tmp_path / "host-copy"
    shutil.copytree(repo, host, ignore=shutil.ignore_patterns(".git"))
    docker_dir = tmp_path / "Docker"
    docker_dir.mkdir()

    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    for name, body in (("ssh", FAKE_SSH), ("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        (fake / "bin" / name).write_text(body)
        (fake / "bin" / name).chmod(0o755)

    e = {
        **os.environ,
        "PATH": f"{fake / 'bin'}:{os.environ['PATH']}",
        "FAKE": str(fake), "FAKE_NEW": VERSION,
        "FOLIO_HOST": "testhost",
        "FOLIO_REMOTE_REPO": str(host),
        "FOLIO_REMOTE_DOCKER": str(docker_dir),
        "FOLIO_CADDY_ENV": "/etc/test/caddy.env",
        "FOLIO_PUBLIC_URL": "https://folio.test",
    }
    return {"repo": repo, "host": host, "docker_dir": docker_dir, "fake": fake, "env": e}


def run(env, *args, timeout=60, **extra):
    t0 = time.monotonic()
    r = subprocess.run(["bash", str(env["repo"] / "scripts" / "deploy.sh"), *args],
                       env={**env["env"], **extra}, capture_output=True, text=True,
                       timeout=timeout)
    r.elapsed = time.monotonic() - t0
    r.out = r.stdout + r.stderr
    return r


def docker_calls(env):
    log = env["fake"] / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


# ---------------------------------------------------------------------------
# Deploying
# ---------------------------------------------------------------------------


def test_deploy_recreates_only_folio_and_verifies(env):
    r = run(env)
    assert r.returncode == 0, r.out
    calls = docker_calls(env)
    assert calls[0] == (f"docker compose up -d --build folio | "
                        f"CADDY_ENV_FILE=/etc/test/caddy.env | cwd={env['docker_dir']}")
    assert [c.split(" | ")[0] for c in calls[1:]] == ["docker exec folio id -u"]
    assert not any("remove-orphans" in c or " down" in c for c in calls)
    assert f"serves {VERSION} as uid 1000, 433 scores" in r.out
    assert f"https://folio.test serves {VERSION}" in r.out


def test_build_only_leaves_the_container_alone(env):
    r = run(env, "--build-only")
    assert r.returncode == 0, r.out
    assert [c.split(" | ")[0] for c in docker_calls(env)] == ["docker compose build folio"]
    assert "running container is unchanged (live: 1.0.0)" in r.out


def test_check_changes_nothing(env):
    r = run(env, "--check")
    assert r.returncode == 0, r.out
    assert docker_calls(env) == []
    assert f"live on testhost: 1.0.0; would deploy {VERSION}" in r.out


def test_a_container_running_as_root_fails_the_deploy(env):
    r = run(env, FAKE_UID="0")
    assert r.returncode == 1
    assert "runs as uid 0, not 1000" in r.out


def test_an_empty_library_fails_the_deploy(env):
    r = run(env, FAKE_SCORES="0")
    assert r.returncode == 1
    assert "the library is empty" in r.out


def test_no_score_count_fails_with_a_hint_not_a_traceback(env):
    """Regression (review): /api/config failing ended the script under
    set -e with only curl's exit code or a Python traceback."""
    r = run(env, FAKE_CONFIG_FAIL="1")
    assert r.returncode == 1
    assert "/api/config gave no score count" in r.out
    assert "Traceback" not in r.out


def test_an_unreachable_public_url_only_warns(env):
    r = run(env, FAKE_PUBLIC_DOWN="1")
    assert r.returncode == 0, r.out
    assert "warning: https://folio.test answered 'nothing'" in r.out


# ---------------------------------------------------------------------------
# Refusing to deploy
# ---------------------------------------------------------------------------


def test_the_mirror_must_match_before_building(env):
    (env["host"] / "web" / "static" / "sw.js").write_text('const APP_VERSION = "old";\n')
    r = run(env, "--timeout", "1")
    assert r.returncode == 1
    assert "hasn't caught up after 1s" in r.out
    assert "web/static/sw.js" in r.out                  # names the differing file
    assert docker_calls(env) == []


def test_it_deploys_once_the_mirror_catches_up(env):
    sw = env["host"] / "web" / "static" / "sw.js"
    good = sw.read_text()
    sw.write_text("stale\n")
    proc = subprocess.Popen(["bash", str(env["repo"] / "scripts" / "deploy.sh"), "--check",
                             "--timeout", "30"], env=env["env"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(2)
    sw.write_text(good)                                  # Syncthing catches up
    out, _ = proc.communicate(timeout=30)
    assert proc.returncode == 0, out
    assert "mirror is in sync" in out


def test_syncthing_temp_files_are_ignored_and_reported(env):
    stray = env["host"] / "web" / "static" / ".syncthing.sw.js.tmp"
    stray.write_text("half a file")
    r = run(env, "--check", "--timeout", "1")
    assert r.returncode == 0, r.out
    assert "web/static/.syncthing.sw.js.tmp" in r.out


def test_uncommitted_build_inputs_are_refused(env):
    (env["repo"] / "Dockerfile").write_text("FROM python:3.13-slim\n")
    r = run(env, "--check")
    assert r.returncode == 1
    assert "uncommitted changes to the build inputs" in r.out and "Dockerfile" in r.out
    shutil.copy(env["repo"] / "Dockerfile", env["host"] / "Dockerfile")
    assert run(env, "--check", "--allow-dirty").returncode == 0


def test_uncommitted_files_outside_the_build_inputs_are_fine(env):
    (env["repo"] / "notes.txt").write_text("scratch\n")
    assert run(env, "--check").returncode == 0


def test_mismatched_versions_are_refused(env):
    (env["repo"] / "web" / "server.py").write_text('title="Folio", version="1.2.3",\n')
    r = run(env, "--check", "--allow-dirty")
    assert r.returncode == 1
    assert f"sw.js {VERSION}, server.py 1.2.3" in r.out


def test_an_unreachable_host_fails_fast(env):
    """Regression (review): an ssh failure looked like a lagging mirror, so it
    waited out the whole timeout and then blamed Syncthing."""
    r = run(env, "--check", "--timeout", "20", FAKE_SSH_FAIL="1")
    assert r.returncode == 1
    assert "can't reach testhost over ssh" in r.out
    assert "No route to host" in r.out                  # ssh's own error shown
    assert "Syncthing" not in r.out
    assert r.elapsed < 10


def test_a_missing_repo_on_the_host_fails_fast(env):
    r = run(env, "--check", "--timeout", "20", FOLIO_REMOTE_REPO="/nonexistent/Folio")
    assert r.returncode == 1
    assert "/nonexistent/Folio doesn't exist on testhost" in r.out
    assert r.elapsed < 10
