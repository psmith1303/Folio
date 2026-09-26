"""Preference and CSS-scale helpers shared across modules (web/static/modules/utils.js).

readPref/writePref centralise the try/catch around localStorage that
annotations.js and pen-style.js each used to duplicate (localStorage.getItem
/setItem can throw, e.g. in private browsing, or the global can be absent
entirely); cssPerPt centralises the "page not open yet" cssW/pdfW ratio used
by stamps.js, annotations.js and pen-style.js. Both are exercised indirectly
via the modules that call them (test_pen_style.py, test_annotation_tools.py);
here they're tested directly, including the localStorage-unavailable paths
nothing else covers.

Runs the REAL shipped functions sliced out of utils.js under Deno; see
deno_harness.py. Deno has a real, persistent localStorage, so every test
here that touches it replaces the global via Object.defineProperty first.
"""

import json

import pytest

from deno_harness import requires_deno, run_deno
from test_annotation_tools import ANNOTATIONS_JS, UTILS_JS, _fn

pytestmark = requires_deno


def _run(body: str, *, extra: str = ""):
    src = UTILS_JS.read_text(encoding="utf-8")
    return run_deno("\n".join([
        _fn(src, "readPref"),
        _fn(src, "writePref"),
        _fn(src, "cssPerPt"),
        extra,
        body,
    ]))


@pytest.mark.parametrize("cssW, pdfW, expected", [
    (600, 400, 1.5),
    (600, 0, 1),        # pdfW 0: page geometry not known yet
    (600, None, 1),      # pdfW undefined: same
    (None, None, 1),
])
def test_cssPerPt_falls_back_to_1_without_a_known_pdf_width(cssW, pdfW, expected):
    r = _run(f"console.log(JSON.stringify(cssPerPt({json.dumps(cssW)}, {json.dumps(pdfW)})));")
    assert r == expected


def test_readPref_returns_null_when_getItem_throws():
    r = _run(
        """console.log(JSON.stringify(readPref("x")));""",
        extra="""
Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
  getItem: () => { throw new Error("blocked"); },
  setItem: () => { throw new Error("blocked"); },
} });
""",
    )
    assert r is None


def test_writePref_does_not_throw_when_setItem_throws():
    r = _run(
        """writePref("x", "1"); console.log(JSON.stringify("ok"));""",
        extra="""
Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
  getItem: () => null,
  setItem: () => { throw new Error("blocked"); },
} });
""",
    )
    assert r == "ok"


def test_readPref_returns_null_when_localStorage_is_undefined():
    r = _run(
        """console.log(JSON.stringify(readPref("x")));""",
        extra='Object.defineProperty(globalThis, "localStorage", '
              '{ configurable: true, value: undefined });',
    )
    assert r is None


def test_writePref_does_not_throw_when_localStorage_is_undefined():
    r = _run(
        """writePref("x", "1"); console.log(JSON.stringify("ok"));""",
        extra='Object.defineProperty(globalThis, "localStorage", '
              '{ configurable: true, value: undefined });',
    )
    assert r == "ok"


def test_readPref_writePref_round_trip_through_a_working_store():
    r = _run(
        """writePref("k", "v"); console.log(JSON.stringify(readPref("k")));""",
        extra="""
const __store = new Map();
Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
  getItem: (k) => (__store.has(k) ? __store.get(k) : null),
  setItem: (k, v) => __store.set(k, String(v)),
} });
""",
    )
    assert r == "v"


def test_the_pencil_only_pref_round_trips_through_readPref_writePref():
    """annotations.js's setPencilOnly/loadPencilOnlyPref used to hand-roll
    the try/catch this replaced; check the switch to readPref/writePref
    didn't change what actually gets stored or read back."""
    annot_src = ANNOTATIONS_JS.read_text(encoding="utf-8")
    utils_src = UTILS_JS.read_text(encoding="utf-8")
    script = "\n".join([
        _fn(utils_src, "readPref"),
        _fn(utils_src, "writePref"),
        'const PENCIL_ONLY_STORAGE_KEY = "folio.pencilOnly";',
        "const btnPencilOnly = { classList: { toggle: () => {} } };",
        "const __s = { pencilOnly: false };",
        "const getState = () => __s;",
        _fn(annot_src, "setPencilOnly"),
        _fn(annot_src, "loadPencilOnlyPref"),
        """
const __store = new Map();
Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
  getItem: (k) => (__store.has(k) ? __store.get(k) : null),
  setItem: (k, v) => __store.set(k, String(v)),
} });
setPencilOnly(true);
const on = loadPencilOnlyPref();
setPencilOnly(false);
const off = loadPencilOnlyPref();
console.log(JSON.stringify({ on, off, stored: __store.get("folio.pencilOnly") }));
""",
    ])
    r = run_deno(script)
    assert r == {"on": True, "off": False, "stored": "0"}
