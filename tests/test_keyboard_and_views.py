"""Keyboard shortcut dispatch and list-view navigation (cleanup Step 8).

Step 8 replaced the modules' injected callbacks with direct imports, sent
the nav bar and its shortcuts through views.navigate(view), and turned
keyboard.js's if-chains and showView's switch into tables. These tests pin
what each key does and what navigating does, so the tables can't drift.

Runs the REAL keyboard.js / views.js under Deno with their sibling modules
stubbed; see js_module_harness.py.
"""

import json

import pytest

from deno_harness import requires_deno
from js_module_harness import MODULES, run_module
from web.server import DEFAULT_KEYBINDINGS

pytestmark = requires_deno

KEYBOARD_JS = MODULES / "keyboard.js"
VIEWS_JS = MODULES / "views.js"

VIEWER = {"pdfDoc": {}, "currentView": "viewer", "displayMode": "fit",
          "activeTool": "nav", "totalPages": 9, "pseudoFullscreen": False}
PD = "preventDefault"

# ---------------------------------------------------------------------------
# keyboard.js
# ---------------------------------------------------------------------------

SCROLLERS = {"library": "library-table-wrap", "setlists": "setlist-list-wrap",
             "recent": "recent-table-wrap"}


def _keys(presses: list, state: dict | None = None) -> dict:
    """Press each (key, modifiers) in turn; return the actions taken (the
    listScroller / isPlacementTool lookups are queries, so they're dropped)
    and the list scroll positions."""
    return run_module(KEYBOARD_JS, ["dom", "state", "annotations", "viewer",
                                    "dialog-handlers", "views"], f"""
__returns.listScroller = (v) => ({json.dumps(SCROLLERS)})[v] ?? null;
__returns.isPlacementTool = (t) => t === "stamp" || t === "startpage";
M.setKeybindings({json.dumps(DEFAULT_KEYBINDINGS)});
M.initKeyboardShortcuts();
for (const [k, mods] of {json.dumps(presses)}) __key(k, mods);
console.log(JSON.stringify({{
  log: __log.filter((e) => !/^(views:listScroller|annotations:isPlacementTool)/.test(e)),
  scroll: Object.fromEntries({json.dumps(list(SCROLLERS.values()) + ["newest-table-wrap"])}
    .map((id) => [id, __el("#" + id).scrollTop])),
}}));
""", state={**VIEWER, **(state or {})})


@pytest.mark.parametrize("key, mods, expected", [
    ("v", {}, ['annotations:setTool:["nav"]']),
    ("d", {}, ['annotations:setTool:["pen"]']),
    ("t", {}, ['annotations:setTool:["text"]']),
    ("e", {}, ['annotations:setTool:["eraser"]']),
    ("m", {}, ['annotations:setTool:["move"]']),
    ("f", {}, ["viewer:toggleFullscreen"]),
    ("s", {}, ["dialog-handlers:showSetlistPicker"]),
    ("g", {}, ["dialog-handlers:showTagEditor"]),
    ("r", {}, ["annotations:rotatePage:[90]"]),
    ("r", {"shift": True}, ["annotations:rotatePage:[-90]"]),
    ("z", {"ctrl": True}, [PD, "annotations:doUndo"]),
    ("z", {"meta": True}, [PD, "annotations:doUndo"]),
    ("ArrowRight", {}, [PD, "viewer:nextPage"]),
    ("ArrowLeft", {}, [PD, "viewer:prevPage"]),
    ("Home", {}, [PD, "viewer:goToPage:[1]"]),
    ("End", {}, [PD, "viewer:goToPage:[9]"]),
    ("Escape", {}, ["viewer:closeScore"]),
    ("q", {}, []),
])
def test_viewer_shortcuts(key, mods, expected):
    assert _keys([[key, mods]])["log"] == expected


@pytest.mark.parametrize("key", ["ArrowDown", " ", "n", "PageDown"])
def test_built_in_next_page_keys(key):
    assert _keys([[key, {}]])["log"] == [PD, "viewer:nextPage"]


@pytest.mark.parametrize("key", ["ArrowUp", "Backspace", "p", "PageUp"])
def test_built_in_prev_page_keys(key):
    assert _keys([[key, {}]])["log"] == [PD, "viewer:prevPage"]


@pytest.mark.parametrize("key, expected", [
    ("ArrowDown", []), ("ArrowUp", []), (" ", []),          # scroll natively
    ("ArrowRight", [PD, "viewer:nextPage"]),
    ("d", ['annotations:setTool:["pen"]']),                  # tools still first
])
def test_wide_mode_lets_vertical_keys_scroll(key, expected):
    assert _keys([[key, {}]], {"displayMode": "wide"})["log"] == expected


def test_escape_cancels_a_placement_tool_before_closing():
    assert _keys([["Escape", {}]], {"activeTool": "stamp"})["log"] == ['annotations:setTool:["nav"]']


def test_escape_leaves_pseudo_fullscreen_before_closing():
    assert _keys([["Escape", {}]], {"pseudoFullscreen": True})["log"] == ["viewer:applyFullscreen:[false]"]


@pytest.mark.parametrize("key, mods, expected", [
    ("l", {"alt": True}, [PD, 'views:navigate:["library"]']),
    ("s", {"alt": True}, [PD, 'views:navigate:["setlists"]']),
    ("r", {"alt": True}, [PD, 'views:navigate:["recent"]']),
    ("n", {"alt": True}, [PD, 'views:navigate:["newest"]']),
    ("f", {"ctrl": True}, [PD, "focus:searchInput"]),
    ("r", {"ctrl": True}, [PD, "click:btnReset"]),
])
@pytest.mark.parametrize("where", ["viewer", "library", "input"])
def test_global_shortcuts_work_everywhere(key, mods, expected, where):
    state = {"pdfDoc": None, "currentView": "library"} if where == "library" else {}
    if where == "input":
        mods = {**mods, "tag": "INPUT"}
    assert _keys([[key, mods]], state)["log"] == expected


def test_typing_in_a_field_takes_no_viewer_shortcuts():
    assert _keys([["d", {"tag": "INPUT"}], ["ArrowRight", {"tag": "TEXTAREA"}]])["log"] == []


def test_escape_in_a_field_just_leaves_the_field():
    assert _keys([["Escape", {"tag": "INPUT"}]])["log"] == ["blur", PD]


@pytest.mark.parametrize("view", ["library", "setlists", "recent"])
def test_home_and_end_scroll_the_list(view):
    r = _keys([["End", {}]], {"pdfDoc": None, "currentView": view})
    assert r["log"] == [PD]
    assert r["scroll"][SCROLLERS[view]] == 500
    r = _keys([["End", {}], ["Home", {}]], {"pdfDoc": None, "currentView": view})
    assert r["scroll"][SCROLLERS[view]] == 0


def test_newest_has_no_list_scrolling():
    r = _keys([["End", {}]], {"pdfDoc": None, "currentView": "newest"})
    assert r["log"] == [] and set(r["scroll"].values()) == {0}


def test_list_views_take_no_viewer_shortcuts():
    assert _keys([["d", {}], ["ArrowRight", {}], ["Escape", {}]],
                 {"pdfDoc": None, "currentView": "library"})["log"] == []


# ---------------------------------------------------------------------------
# views.js
# ---------------------------------------------------------------------------

LIST_VIEWS = {  # view -> (element, button, loader stub)
    "library": ("libraryView", "btnLibrary", "library:loadLibrary"),
    "setlists": ("setlistView", "btnSetlists", "setlists:loadSetlists"),
    "recent": ("recentView", "btnRecent", "recent:renderRecent"),
    "newest": ("newestView", "btnNewest", "newest:renderNewest"),
}
PANES = [v[0] for v in LIST_VIEWS.values()] + ["viewerView"]
BUTTONS = [v[1] for v in LIST_VIEWS.values()]


def _views(body: str, state: dict | None = None) -> dict:
    return run_module(VIEWS_JS, ["dom", "state", "viewer", "library", "setlists",
                                 "recent", "newest"], f"""
__el("titleDisplay").textContent = "Score title";
{body}
console.log(JSON.stringify({{
  log: __log,
  view: __state.currentView,
  shown: {json.dumps(PANES)}.filter((n) => !__el(n).classList.contains("hidden")),
  active: {json.dumps(BUTTONS)}.filter((n) => __el(n).classList.contains("active")),
  title: __el("titleDisplay").textContent,
}}));
""", state={"currentView": "library", "appTitle": "Folio", **(state or {})})


@pytest.mark.parametrize("view", LIST_VIEWS)
def test_navigate_from_the_viewer_closes_the_score_then_loads_the_view(view):
    el, button, loader = LIST_VIEWS[view]
    r = _views(f'M.navigate("{view}");', {"currentView": "viewer"})
    assert r == {"log": ["viewer:cleanupScore", loader], "view": view,
                 "shown": [el], "active": [button], "title": "Folio"}


@pytest.mark.parametrize("start", ["library", "setlists", "recent", "newest"])
def test_navigate_between_list_views_keeps_nothing_to_close(start):
    r = _views('M.navigate("recent");', {"currentView": start})
    assert r["log"] == ["recent:renderRecent"]
    assert r["shown"] == ["recentView"] and r["active"] == ["btnRecent"]


def test_switching_views_hides_the_previous_one():
    r = _views('M.showView("library"); M.showView("setlists");')
    assert r["shown"] == ["setlistView"] and r["active"] == ["btnSetlists"]


def test_showing_the_viewer_hides_every_list_and_keeps_the_title():
    r = _views('M.showView("library"); M.showView("viewer");')
    assert r["shown"] == ["viewerView"] and r["active"] == []
    assert r["log"] == ["focus:pdfContainer"]
    assert r["title"] == "Folio"          # set by the library, left alone by the viewer
    assert r["view"] == "viewer"


def test_nav_buttons_navigate():
    r = _views("""
M.initNavButtons();
for (const b of ["btnNewest", "btnLibrary"]) __el(b).listeners.click();
""", {"currentView": "viewer"})
    assert r["log"] == ["viewer:cleanupScore", "newest:renderNewest", "library:loadLibrary"]
    assert r["shown"] == ["libraryView"]


@pytest.mark.parametrize("view, expected", [
    ("library", "library-table-wrap"), ("setlists", "setlist-list-wrap"),
    ("recent", "recent-table-wrap"), ("newest", None), ("viewer", None),
    ("toString", None),
])
def test_list_scroller(view, expected):
    r = run_module(VIEWS_JS, ["dom", "state", "viewer", "library", "setlists", "recent", "newest"],
                   f'console.log(JSON.stringify(M.listScroller("{view}")));')
    assert r == expected
