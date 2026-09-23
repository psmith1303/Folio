"""Start-page stamp: which page a score opens on, and the setlist page range.

Runs the REAL shipped JavaScript under Deno (utils.js is imported directly;
getPageRange is sliced out of viewer.js with only getState stubbed), in the
same spirit as test_offline_boot_fixes.py (see deno_harness.py).
"""

import json
from pathlib import Path

import pytest

from deno_harness import requires_deno, run_deno, slice_source

MODULES = Path(__file__).resolve().parent.parent / "web" / "static" / "modules"
UTILS_JS = MODULES / "utils.js"
VIEWER_JS = MODULES / "viewer.js"

pytestmark = requires_deno


def _call(expr: str):
    """Evaluate *expr* with the utils.js exports in scope."""
    return run_deno(
        f'import {{ findStartPage, songStartPage }} from "{UTILS_JS.as_uri()}";\n'
        f"console.log(JSON.stringify({expr}));\n"
    )


def _stamp(**kw):
    return {"type": "startpage", "x": 0.5, "y": 0.5, **kw}


INK = {"type": "ink", "points": [[0, 0], [1, 1]]}


# ---------------------------------------------------------------------------
# findStartPage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pages, expected", [
    ({}, None),
    (None, None),
    ({"0": [INK], "3": []}, None),
    ({"2": [INK, _stamp()]}, 3),            # 0-based key -> 1-based page
    ({"0": [_stamp()]}, 1),
    ({"10": [_stamp()], "9": [_stamp()]}, 10),  # numeric, not string, order
])
def test_find_start_page(pages, expected):
    assert _call(f"findStartPage({json.dumps(pages)})") == expected


# ---------------------------------------------------------------------------
# songStartPage — start_page 1 means "not specified"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("song, pages, expected", [
    ({"start_page": 1}, {"4": [_stamp()]}, 5),   # stamp wins over the default
    ({}, {"4": [_stamp()]}, 5),                  # missing start_page == 1
    ({"start_page": 3}, {"4": [_stamp()]}, 3),   # explicit start_page wins
    ({"start_page": 1}, {}, 1),                  # no stamp -> first page
])
def test_song_start_page(song, pages, expected):
    assert _call(f"songStartPage({json.dumps(song)}, {json.dumps(pages)})") == expected


# ---------------------------------------------------------------------------
# getPageRange (viewer.js) — going back from the stamp leaves the song
# ---------------------------------------------------------------------------

def _page_range(state: dict):
    fn = slice_source(VIEWER_JS.read_text(encoding="utf-8"),
                      "export function getPageRange()",
                      "export function goToPage").replace("export function", "function")
    return run_deno(
        f'import {{ songStartPage }} from "{UTILS_JS.as_uri()}";\n'
        f"const _s = {json.dumps(state)};\n"
        "const getState = () => _s;\n"
        f"{fn}\n"
        "console.log(JSON.stringify(getPageRange()));\n"
    )


def _playback(song):
    return {"songs": [song], "index": 0}


def test_range_outside_setlist_ignores_stamp():
    r = _page_range({"setlistPlayback": None, "totalPages": 8,
                     "annotations": {"2": [_stamp()]}})
    assert r == {"min": 1, "max": 8}


def test_range_starts_at_stamp_in_setlist():
    r = _page_range({"setlistPlayback": _playback({"start_page": 1, "end_page": None}),
                     "totalPages": 8, "annotations": {"2": [_stamp()]}})
    assert r == {"min": 3, "max": 8}


def test_range_explicit_start_page_beats_stamp():
    r = _page_range({"setlistPlayback": _playback({"start_page": 2, "end_page": None}),
                     "totalPages": 8, "annotations": {"5": [_stamp()]}})
    assert r == {"min": 2, "max": 8}


def test_range_stamp_past_end_page_is_clamped():
    r = _page_range({"setlistPlayback": _playback({"start_page": 1, "end_page": 2}),
                     "totalPages": 8, "annotations": {"5": [_stamp()]}})
    assert r == {"min": 2, "max": 2}
