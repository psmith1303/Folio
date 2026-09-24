"""While any dialog is open, keys must not act on the score behind it.

keyboard.js ignores viewer shortcuts while a dialog is open. It used to
check a hand-kept list of dialogs, which missed the clear-page
confirmation: with it open, arrow keys turned pages and tool keys switched
tools behind it. The check now asks the page for any open <dialog>, so
every dialog in index.html is covered, including ones added later.

Runs the REAL keyboard.js under Deno with its sibling modules stubbed; see
js_module_harness.py.
"""

import re
from pathlib import Path

import pytest

from deno_harness import requires_deno
from js_module_harness import MODULES, run_module

pytestmark = requires_deno

INDEX_HTML = Path(__file__).resolve().parent.parent / "web" / "static" / "index.html"
KEYBOARD_JS = MODULES / "keyboard.js"
SIBLINGS = ["dom", "state", "annotations", "viewer", "dialog-handlers", "views"]

KEYBINDINGS = {
    "tool_pen": "d", "close_score": "Escape", "undo": "Ctrl+z",
    "next_page": "ArrowRight", "prev_page": "ArrowLeft", "first_page": "Home",
}
VIEWER = {"pdfDoc": {}, "currentView": "viewer", "displayMode": "fit",
          "activeTool": "nav", "totalPages": 9, "pseudoFullscreen": False}
# One key per kind of viewer shortcut: page turns (bound and built-in),
# a tool key, Escape (closes the score) and an undo combo.
KEYS = ["ArrowRight", "ArrowDown", " ", "PageDown", "Home", "d", "Escape"]


def _camel(dialog_id: str) -> str:
    head, *rest = dialog_id.split("-")
    return head + "".join(w.capitalize() for w in rest)


DIALOGS = [_camel(i) for i in re.findall(r'<dialog[^>]*\bid="([^"]+)"',
                                         INDEX_HTML.read_text(encoding="utf-8"))]


def _press(open_dialog: str | None, keys: list[str]) -> list[str]:
    opened = f'__el("{open_dialog}").open = true;' if open_dialog else ""
    return run_module(KEYBOARD_JS, SIBLINGS, f"""
M.setKeybindings({KEYBINDINGS!r});
M.initKeyboardShortcuts();
{opened}
for (const k of {keys!r}) __key(k);
__key("z", {{ ctrl: true }});
console.log(JSON.stringify(__log));
""", state=VIEWER)


def test_every_dialog_in_the_page_is_found():
    assert len(DIALOGS) >= 11 and "clearPageDialog" in DIALOGS


def test_with_no_dialog_open_the_keys_act_on_the_score():
    """The harness reaches the real handler: without a dialog every key acts."""
    log = _press(None, KEYS)
    assert "viewer:nextPage" in log and "viewer:goToPage:[1]" in log
    assert 'annotations:setTool:["pen"]' in log and "viewer:closeScore" in log
    assert "annotations:doUndo" in log


@pytest.mark.parametrize("dialog", DIALOGS)
def test_an_open_dialog_blocks_viewer_shortcuts(dialog):
    """Regression: the clear-page confirmation wasn't in the list, so keys
    turned pages, switched tools and closed the score behind it."""
    assert _press(dialog, KEYS) == []
