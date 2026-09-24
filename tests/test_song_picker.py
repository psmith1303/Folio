"""The setlist song picker searches the in-memory library (Step 5).

It used to fetch /api/library?q=... per keystroke, which failed offline. Now
it filters the scores the library view already loaded, so it must be able to
tell "no matches" from "the library isn't loaded" -- including after the
library is switched and the new list hasn't arrived.

Runs the REAL shipped source under Deno (functions sliced out of setlists.js
and dialog-handlers.js, library-filter.js imported), with only the DOM
stubbed; see deno_harness.py.
"""

import json
import re
from pathlib import Path

from deno_harness import requires_deno, run_deno, slice_source

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
MODULES = STATIC / "modules"
SETLISTS_JS = MODULES / "setlists.js"
DIALOGS_JS = MODULES / "dialog-handlers.js"
FILTER_JS = MODULES / "library-filter.js"
SW_JS = STATIC / "sw.js"

SCORES = [
    {"filepath": "/m/Mozart - Sonata.pdf", "composer": "Mozart", "title": "Sonata", "tags": []},
    {"filepath": "/m/Bach - Suite.pdf", "composer": "Bach", "title": "Suite", "tags": []},
    {"filepath": "/m/Bach - Air.pdf", "composer": "Bach", "title": "Air", "tags": []},
]

# A list element that records its rendered rows.
DOM_STUBS = """
const document = {
  createElement: () => ({
    className: "", textContent: "", classList: { add() {}, remove() {} },
    addEventListener() {},
  }),
};
const songPickerList = {
  rows: [], _html: "",
  get innerHTML() { return this._html; },
  set innerHTML(v) { this._html = v; this.rows = []; },
  appendChild(el) { this.rows.push(el.textContent); },
  querySelectorAll: () => [],
};
const songPickerAdd = { disabled: true };
"""


def _render_picker(state: dict, query: str) -> dict:
    fn = slice_source(SETLISTS_JS.read_text(encoding="utf-8"),
                      "function renderSongPicker(query) {", "\n}\n") + "\n}\n"
    return run_deno(f"""
import {{ searchScores }} from "{FILTER_JS.as_uri()}";
{DOM_STUBS}
const __s = {json.dumps(state)};
const getState = () => __s;
{fn}
renderSongPicker({json.dumps(query)});
console.log(JSON.stringify({{ rows: songPickerList.rows, html: songPickerList.innerHTML }}));
""")


@requires_deno
def test_picker_searches_the_loaded_library():
    r = _render_picker({"libraryLoaded": True, "allScores": SCORES}, " bach")
    assert r == {"rows": ["Bach — Air", "Bach — Suite"], "html": ""}


@requires_deno
def test_picker_with_no_matches_is_empty_not_an_error():
    r = _render_picker({"libraryLoaded": True, "allScores": SCORES}, "zzz")
    assert r == {"rows": [], "html": ""}


@requires_deno
def test_picker_says_when_the_library_is_not_loaded():
    """Regression: with no library in memory (e.g. offline with nothing
    cached) the picker showed an empty list, which reads as "no matches"."""
    r = _render_picker({"libraryLoaded": False, "allScores": []}, "")
    assert r["rows"] == []
    assert "Library not loaded" in r["html"]


def _switch_library(reload_succeeds: bool) -> dict:
    """Run the directory dialog's close handler with a stubbed API; the
    library reload either repopulates the state or fails (loadLibrary
    reports its own errors and returns normally)."""
    src = DIALOGS_JS.read_text(encoding="utf-8")
    handler = slice_source(src, 'dirDialog.addEventListener("close", async () => {',
                           "\n  });\n") + "\n  });\n"
    return run_deno(f"""
const __s = {{
  selectedTags: new Set(["old"]), libraryLoaded: true,
  allScores: [{{ filepath: "/old/Bach - Suite.pdf" }}],
}};
const getState = () => __s;
const dirInput = {{ value: "/new" }};
const libraryStatus = {{ textContent: "" }};
const api = async () => ({{}});
const refreshCachedConfig = () => {{}};
let seenAtReload = null;
const _loadLibrary = async () => {{
  seenAtReload = {{ allScores: __s.allScores.length, loaded: __s.libraryLoaded }};
  if ({json.dumps(reload_succeeds)}) {{
    __s.allScores = [{{ filepath: "/new/Mozart - Sonata.pdf" }}];
    __s.libraryLoaded = true;
  }}
}};
let onClose;
const dirDialog = {{ addEventListener: (ev, fn) => {{ onClose = fn; }} }};
{handler}
await onClose();
console.log(JSON.stringify({{
  seenAtReload, loaded: __s.libraryLoaded,
  paths: __s.allScores.map((s) => s.filepath), tags: [...__s.selectedTags],
}}));
""")


@requires_deno
def test_switching_library_drops_the_old_scores_before_reloading():
    """Regression: if the new library's list failed to load, the picker kept
    offering the previous library's scores."""
    r = _switch_library(reload_succeeds=False)
    assert r["seenAtReload"] == {"allScores": 0, "loaded": False}
    assert r == {**r, "loaded": False, "paths": [], "tags": []}


@requires_deno
def test_switching_library_then_reloading_shows_the_new_scores():
    r = _switch_library(reload_succeeds=True)
    assert r["loaded"] is True
    assert r["paths"] == ["/new/Mozart - Sonata.pdf"]


def test_every_module_is_in_the_offline_shell():
    """A module missing from SHELL_URLS isn't precached, so the app fails to
    load offline (e.g. the new library-filter.js)."""
    shell = set(re.findall(r'"(/[^"]+)"',
                           slice_source(SW_JS.read_text(encoding="utf-8"),
                                        "const SHELL_URLS = [", "];")))
    modules = {f"/modules/{p.name}" for p in MODULES.glob("*.js")}
    assert modules - shell == set()
