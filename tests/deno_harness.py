"""Run shipped JavaScript under Deno from pytest.

Tests that exercise the real frontend source slice it out of the shipped
file, build a small script around it and run it here; the script prints its
result as JSON on stdout. Imported by bare name: pytest puts tests/ on
sys.path (it has no __init__.py).
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
DENO = shutil.which("deno")
requires_deno = pytest.mark.skipif(DENO is None, reason="deno not installed")


def slice_source(source: str, start_marker: str, end_marker: str) -> str:
    """The text of *source* from *start_marker* up to the next *end_marker*.

    Fails the test, naming the marker, if either is missing -- the shipped
    file changed and the test's markers need updating.
    """
    try:
        start = source.index(start_marker)
        end = source.index(end_marker, start)
    except ValueError as exc:
        raise AssertionError(
            f"marker not found ({start_marker!r} .. {end_marker!r}); the "
            "shipped source changed and the test's markers need updating"
        ) from exc
    return source[start:end]


def run_deno(script: str, *, timeout: int = 60) -> Any:
    """Run *script* under Deno and return its stdout parsed as JSON.

    The script may read files in the repo (e.g. import a shipped module by
    file:// URL). Fails the test if Deno exits non-zero.
    """
    proc = subprocess.run(
        [DENO, "run", "--no-check", f"--allow-read={REPO}", "-"],
        input=script, capture_output=True, text=True, timeout=timeout,
    )
    assert proc.returncode == 0, f"deno failed:\n{proc.stderr}"
    return json.loads(proc.stdout)
