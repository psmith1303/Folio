"""scripts/deploy.sh: deploy Folio by pushing to p3800, then verify.

Runs the real script from a throwaway git repo holding just the build
inputs. Its `p3800` remote is a local bare repo whose pre-receive hook
stands in for p3800's (it can reject) and whose post-receive marks the
deploy; `origin` pushes to a stand-in for GitHub and to that same bare repo,
as the real origin does. `ssh` runs its command locally; `docker` and `curl`
are fakes that answer as the host would.
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
echo "docker $*" >> "$FAKE/docker.log"
[[ "$*" == "exec folio id -u" ]] && echo "${FAKE_UID:-1000}"
exit 0
"""

FAKE_CURL = r"""#!/bin/bash
case "$*" in
  *folio.test*) [[ -n "$FAKE_PUBLIC_DOWN" ]] && exit 7
                echo "{\"info\":{\"version\":\"$FAKE_NEW\"}}" ;;
  *openapi.json*) if [[ -f "$FAKE/deployed" && -z "$FAKE_STUCK" ]]; then v=$FAKE_NEW; else v=1.0.0; fi
                  echo "{\"info\":{\"version\":\"$v\"}}" ;;
  *api/config*) [[ -n "$FAKE_CONFIG_FAIL" ]] && exit 22
                echo "{\"score_count\": ${FAKE_SCORES:-433}}" ;;
  *) exit 22 ;;
esac
"""

# p3800's hook, in miniature: the test step can fail the push; a deploy is a
# marker the fake curl reads.
PRE_RECEIVE = r"""#!/bin/bash
cat >/dev/null
echo "pre-receive" >> "$FAKE/hook.log"
[[ -n "$FAKE_REJECT" ]] && { echo "deploy: folio: test FAILED (exit 1) — push rejected"; exit 1; }
exit 0
"""
POST_RECEIVE = r"""#!/bin/bash
cat >/dev/null
echo "post-receive" >> "$FAKE/hook.log"
touch "$FAKE/deployed"
"""


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(repo, msg):
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg)


@pytest.fixture
def env(tmp_path):
    """A repo on main with the build inputs, remotes p3800 (hooked) and
    origin (GitHub + p3800), both holding an older commit, and the fakes on
    PATH."""
    repo = tmp_path / "Folio"
    (repo / "scripts").mkdir(parents=True)
    (repo / "web" / "static").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "deploy.sh")
    (repo / "web" / "static" / "sw.js").write_text('const APP_VERSION = "1.0.0";\n')
    (repo / "web" / "server.py").write_text('app = FastAPI(\n    title="Folio", version="1.0.0",\n)\n')
    (repo / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (repo / ".dockerignore").write_text("docs/\n")
    (repo / "requirements.txt").write_text("fastapi\n")
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "1.0.0")

    p3800 = tmp_path / "p3800.git"
    github = tmp_path / "github.git"
    for bare in (p3800, github):
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    _git(repo, "remote", "add", "p3800", str(p3800))
    _git(repo, "remote", "add", "origin", str(github))
    _git(repo, "remote", "set-url", "--add", "--push", "origin", str(github))
    _git(repo, "remote", "set-url", "--add", "--push", "origin", str(p3800))
    _git(repo, "push", "-q", "origin", "main")          # before the hooks: "live" is 1.0.0
    for name, body in (("pre-receive", PRE_RECEIVE), ("post-receive", POST_RECEIVE)):
        (p3800 / "hooks" / name).write_text(body)
        (p3800 / "hooks" / name).chmod(0o755)

    (repo / "web" / "static" / "sw.js").write_text(f'const APP_VERSION = "{VERSION}";\n')
    (repo / "web" / "server.py").write_text(f'app = FastAPI(\n    title="Folio", version="{VERSION}",\n)\n')
    _commit(repo, VERSION)

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
        "FOLIO_PUBLIC_URL": "https://folio.test",
    }
    return {"repo": repo, "p3800": p3800, "github": github, "fake": fake, "env": e}


def run(env, *args, timeout=60, **extra):
    t0 = time.monotonic()
    r = subprocess.run(["bash", str(env["repo"] / "scripts" / "deploy.sh"), *args],
                       env={**env["env"], **extra}, capture_output=True, text=True,
                       timeout=timeout)
    r.elapsed = time.monotonic() - t0
    r.out = r.stdout + r.stderr
    return r


def head(bare):
    return _git(bare, "rev-parse", "main")


def hook_runs(env):
    log = env["fake"] / "hook.log"
    return log.read_text().splitlines() if log.exists() else []


def docker_calls(env):
    log = env["fake"] / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


# ---------------------------------------------------------------------------
# Deploying
# ---------------------------------------------------------------------------


def test_deploy_pushes_to_p3800_then_github_and_verifies(env):
    r = run(env)
    assert r.returncode == 0, r.out
    tip = _git(env["repo"], "rev-parse", "HEAD")
    assert head(env["p3800"]) == tip and head(env["github"]) == tip
    assert hook_runs(env) == ["pre-receive", "post-receive"]   # origin's p3800 push: a no-op
    assert docker_calls(env) == ["docker exec folio id -u"]    # it never builds itself
    assert f"serves {VERSION} as uid 1000, 433 scores" in r.out
    assert f"https://folio.test serves {VERSION}" in r.out


def test_a_rejected_push_stops_before_github_and_verifying(env):
    old = head(env["github"])
    r = run(env, FAKE_REJECT="1")
    assert r.returncode == 1
    assert "p3800 rejected the push" in r.out
    assert "test FAILED" in r.out                       # the hook's own words, as remote: lines
    assert head(env["github"]) == old and head(env["p3800"]) == old
    assert docker_calls(env) == []


def test_a_failed_github_push_only_warns(env):
    shutil.rmtree(env["github"])
    r = run(env)
    assert r.returncode == 0, r.out
    assert "the push to origin failed; p3800 is deployed regardless" in r.out
    assert f"serves {VERSION}" in r.out


def test_already_deployed_pushes_nothing_new_and_still_verifies(env):
    subprocess.run(["git", "-C", str(env["repo"]), "push", "-q", "p3800", "main"],
                   env=env["env"], check=True, capture_output=True)
    (env["fake"] / "hook.log").unlink()
    r = run(env)
    assert r.returncode == 0, r.out
    assert hook_runs(env) == []
    assert f"serves {VERSION} as uid 1000" in r.out


def test_check_pushes_nothing(env):
    old = head(env["p3800"])
    r = run(env, "--check")
    assert r.returncode == 0, r.out
    assert head(env["p3800"]) == old and hook_runs(env) == []
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


def test_a_container_still_serving_the_old_version_fails_the_deploy(env):
    """The pushed version never comes up: the deploy fails, naming what is
    served, and goes no further (no uid or library checks)."""
    r = run(env, "--verify-timeout", "2", FAKE_STUCK="1")
    assert r.returncode == 1
    assert f"testhost serves 1.0.0 after 2s, not {VERSION}" in r.out
    assert docker_calls(env) == []
    assert r.elapsed < 10


def test_an_unreachable_public_url_only_warns(env):
    r = run(env, FAKE_PUBLIC_DOWN="1")
    assert r.returncode == 0, r.out
    assert "warning: https://folio.test answered 'nothing'" in r.out


# ---------------------------------------------------------------------------
# Refusing to deploy
# ---------------------------------------------------------------------------


def test_uncommitted_build_inputs_are_refused(env):
    (env["repo"] / "Dockerfile").write_text("FROM python:3.13-slim\n")
    r = run(env)
    assert r.returncode == 1
    assert "uncommitted changes to the build inputs" in r.out and "Dockerfile" in r.out
    assert hook_runs(env) == []
    r = run(env, "--allow-dirty")
    assert r.returncode == 0, r.out
    assert "uncommitted changes NOT included" in r.out


def test_uncommitted_files_outside_the_build_inputs_are_fine(env):
    (env["repo"] / "notes.txt").write_text("scratch\n")
    assert run(env, "--check").returncode == 0


def test_mismatched_versions_are_refused(env):
    (env["repo"] / "web" / "server.py").write_text('title="Folio", version="1.2.3",\n')
    r = run(env, "--check", "--allow-dirty")
    assert r.returncode == 1
    assert f"sw.js {VERSION}, server.py 1.2.3" in r.out


def test_another_branch_is_refused(env):
    _git(env["repo"], "checkout", "-q", "-b", "feature")
    r = run(env)
    assert r.returncode == 1
    assert "not on main" in r.out
    assert hook_runs(env) == []


def test_an_unreachable_host_fails_fast(env):
    r = run(env, FAKE_SSH_FAIL="1")
    assert r.returncode == 1
    assert "can't reach testhost over ssh" in r.out
    assert "No route to host" in r.out                  # ssh's own error shown
    assert hook_runs(env) == []
    assert r.elapsed < 10
