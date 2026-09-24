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


def _edit_tags(*, opened_from_library: bool = False, library: list | None = None,
               put_fails: bool = False, switch_during_put: bool = False) -> dict:
    """Run the tag editor's close handler ("save") on the open score, with a
    stubbed PUT that renames Bach - Suite.pdf, then return the loaded library
    and what the picker offers for "bach"."""
    src = DIALOGS_JS.read_text(encoding="utf-8")
    handler = slice_source(src, 'tagEditorDialog.addEventListener("close", async () => {',
                           "\n  });\n") + "\n  });\n"
    old = "/m/Bach - Suite.pdf"
    renamed = {"filepath": "/m/Bach - Suite -- baroque.pdf", "filename": "Bach - Suite -- baroque.pdf",
               "composer": "Bach", "title": "Suite", "tags": ["baroque"],
               "folder_tags": [], "filename_tags": ["baroque"]}
    lib = SCORES if library is None else library
    return run_deno(f"""
import {{ searchScores }} from "{FILTER_JS.as_uri()}";
{DOM_STUBS}
const __lib = {json.dumps(lib)};
const __s = {{
  libraryLoaded: true, allScores: __lib, setlistPlayback: null,
  _tagEditorLoaded: true, _tagEditorPath: {json.dumps(old)}, _editingFilenameTags: ["baroque"],
}};
// Opened from Recent or a setlist, the viewer holds its own copy of the score.
__s.currentScore = {json.dumps(opened_from_library)}
  ? __lib.find((sc) => sc.filepath === {json.dumps(old)})
  : {{ ...{json.dumps(SCORES[1])} }};
const getState = () => __s;
let __putCalls = 0;
const api = async () => {{
  __putCalls++;
  if ({json.dumps(switch_during_put)}) __s.currentScore = {{ filepath: "/m/Mozart - Sonata.pdf" }};
  if ({json.dumps(put_fails)}) throw new Error("409");
  return {{ ok: true, score: {json.dumps(renamed)} }};
}};
const alert = () => {{}};
console.error = () => {{}};
const titleDisplay = {{ textContent: "" }};
let _tagEditorLoading = false;
let onClose;
const tagEditorDialog = {{ returnValue: "save", addEventListener: (ev, fn) => {{ onClose = fn; }} }};
{handler}
await onClose();
console.log(JSON.stringify({{
  paths: __s.allScores.map((sc) => sc.filepath),
  offered: searchScores(__s.allScores, "suite").map((sc) => sc.filepath),
  viewing: __s.currentScore.filepath,
  saved: !_tagEditorLoading && __s._tagEditorPath === "" && __putCalls > 0,
}}));
""")


@requires_deno
def test_picker_offers_the_renamed_file_after_a_tag_edit():
    """Regression: a score opened from Recent or a setlist is a copy, so a tag
    edit (which renames the file) updated the viewer but not the loaded
    library, and the picker added the old, now missing, path to setlists."""
    r = _edit_tags()
    assert r["offered"] == ["/m/Bach - Suite -- baroque.pdf"]
    assert r["viewing"] == "/m/Bach - Suite -- baroque.pdf"


@requires_deno
def test_tag_edit_replaces_the_library_row_in_place():
    r = _edit_tags()
    assert r["paths"] == ["/m/Mozart - Sonata.pdf", "/m/Bach - Suite -- baroque.pdf",
                          "/m/Bach - Air.pdf"]


@requires_deno
def test_library_row_is_renamed_even_if_the_viewer_moved_on_during_the_save():
    """The file is renamed on disk whatever the viewer shows by the time the
    PUT returns; only the viewer's own record is left alone."""
    r = _edit_tags(switch_during_put=True)
    assert r["offered"] == ["/m/Bach - Suite -- baroque.pdf"]
    assert r["viewing"] == "/m/Mozart - Sonata.pdf"


@requires_deno
def test_score_opened_from_the_library_is_renamed_once():
    """Opened from the library view, the viewer and the library share one
    object; the rename must not duplicate or drop the row."""
    r = _edit_tags(opened_from_library=True)
    assert r["paths"].count("/m/Bach - Suite -- baroque.pdf") == 1
    assert len(r["paths"]) == 3


@requires_deno
def test_failed_tag_save_leaves_the_library_alone():
    r = _edit_tags(put_fails=True)
    assert r["offered"] == ["/m/Bach - Suite.pdf"]


@requires_deno
def test_tag_edit_with_the_score_missing_from_the_library_adds_nothing():
    lib = [s for s in SCORES if s["title"] != "Suite"]
    r = _edit_tags(library=lib + [{**SCORES[1], "filepath": "/elsewhere/x.pdf"}])
    assert r["saved"] and r["viewing"] == "/m/Bach - Suite -- baroque.pdf"
    assert "/m/Bach - Suite -- baroque.pdf" not in r["paths"]
    assert len(r["paths"]) == 3
