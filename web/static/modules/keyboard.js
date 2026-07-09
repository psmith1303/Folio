// ---------------------------------------------------------------------------
// Keyboard shortcuts — configurable, data-driven keydown handler
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import {
  textDialog, dirDialog, setlistNameDialog, songPickerDialog,
  setlistPickerDialog, setlistRefPickerDialog,
  conflictDialog, offlineDialog, stampDialog,
  btnLibrary, btnSetlists, btnRecent, btnNewest, btnReset, searchInput,
} from "./dom.js";
import { setTool, doUndo } from "./annotations.js";
import {
  nextPage, prevPage, goToPage, closeScore,
  toggleFullscreen, applyFullscreen,
} from "./viewer.js";
import { showSetlistPicker, showTagEditor } from "./dialog-handlers.js";
import { rotatePage } from "./annotations.js";

// ---------------------------------------------------------------------------
// Keybinding matching
// ---------------------------------------------------------------------------

/**
 * Parse a binding string like "Alt+l" or "Ctrl+Shift+r" into a descriptor.
 */
function parseBinding(str) {
  const parts = str.split("+");
  const key = parts.pop();
  const mods = new Set(parts.map((m) => m.toLowerCase()));
  return { key, ctrl: mods.has("ctrl"), alt: mods.has("alt"), shift: mods.has("shift"), meta: mods.has("meta") };
}

function matchesBinding(e, binding) {
  if (!binding) return false;
  const b = typeof binding === "string" ? parseBinding(binding) : binding;
  // For single-char keys, compare case-sensitively; for named keys, case-insensitive
  const keyMatch = b.key.length === 1
    ? e.key === b.key
    : e.key.toLowerCase() === b.key.toLowerCase();
  // Treat Ctrl and Meta as interchangeable (Ctrl on Windows/Linux, Cmd on Mac)
  const ctrlOrMeta = e.ctrlKey || e.metaKey;
  return keyMatch
    && (b.ctrl ? ctrlOrMeta : (!e.ctrlKey && !e.metaKey))
    && e.altKey === b.alt
    && e.shiftKey === b.shift;
}

// ---------------------------------------------------------------------------
// Keybindings state — populated from server config
// ---------------------------------------------------------------------------

let _bindings = {};
let _parsed = {};

export function setKeybindings(bindings) {
  _bindings = bindings;
  _parsed = {};
  for (const [action, str] of Object.entries(bindings)) {
    _parsed[action] = parseBinding(str);
  }
}

function matches(e, action) {
  return matchesBinding(e, _parsed[action]);
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

function isDialogOpen() {
  return (
    textDialog.open || dirDialog.open || setlistNameDialog.open ||
    songPickerDialog.open || setlistPickerDialog.open ||
    setlistRefPickerDialog.open || conflictDialog.open ||
    offlineDialog.open || stampDialog.open
  );
}

function handleGlobalShortcuts(e) {
  // Global shortcuts work even from non-viewer views and input fields
  // (Alt+ and Ctrl+ combos don't conflict with typing)
  if (matches(e, "go_library")) {
    e.preventDefault();
    btnLibrary.click();
    return true;
  }
  if (matches(e, "go_setlists")) {
    e.preventDefault();
    btnSetlists.click();
    return true;
  }
  if (matches(e, "go_recent")) {
    e.preventDefault();
    btnRecent.click();
    return true;
  }
  if (matches(e, "go_newest")) {
    e.preventDefault();
    btnNewest.click();
    return true;
  }
  if (matches(e, "focus_search")) {
    e.preventDefault();
    searchInput.focus();
    searchInput.select();
    return true;
  }
  if (matches(e, "reset_filters")) {
    e.preventDefault();
    btnReset.click();
    return true;
  }
  return false;
}

function handleViewerShortcuts(e) {
  const s = getState();

  // Tool shortcuts
  if (matches(e, "tool_nav")) { setTool("nav"); return true; }
  if (matches(e, "tool_pen")) { setTool("pen"); return true; }
  if (matches(e, "tool_text")) { setTool("text"); return true; }
  if (matches(e, "tool_eraser")) { setTool("eraser"); return true; }
  if (matches(e, "tool_move")) { setTool("move"); return true; }
  if (matches(e, "toggle_fullscreen")) { toggleFullscreen(); return true; }
  if (matches(e, "add_to_setlist")) { showSetlistPicker(); return true; }
  if (matches(e, "edit_tags")) { showTagEditor(); return true; }
  if (matches(e, "rotate_cw")) { rotatePage(90); return true; }
  if (matches(e, "rotate_ccw")) { rotatePage(-90); return true; }
  if (matches(e, "undo")) { e.preventDefault(); doUndo(); return true; }

  // In wide mode, let arrow up/down and space scroll natively
  if (s.displayMode === "wide" && (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === " ")) {
    return false;
  }

  // Page navigation — match configured bindings plus built-in alternates
  if (matches(e, "next_page") || (["ArrowDown", " ", "n", "PageDown"].includes(e.key) && !e.ctrlKey && !e.altKey)) {
    e.preventDefault();
    nextPage();
    return true;
  }
  if (matches(e, "prev_page") || (["ArrowUp", "Backspace", "p", "PageUp"].includes(e.key) && !e.ctrlKey && !e.altKey)) {
    e.preventDefault();
    prevPage();
    return true;
  }
  if (matches(e, "first_page")) { e.preventDefault(); goToPage(1); return true; }
  if (matches(e, "last_page")) { e.preventDefault(); goToPage(s.totalPages); return true; }

  if (matches(e, "close_score")) {
    // Escape first cancels stamp-placement mode, before closing the score.
    if (s.activeTool === "stamp") { setTool("nav"); return true; }
    if (s.pseudoFullscreen) applyFullscreen(false);
    else closeScore();
    return true;
  }

  return false;
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

export function initKeyboardShortcuts() {
  document.addEventListener("keydown", (e) => {
    // Global shortcuts (Alt+/Ctrl+ combos) work from anywhere, even inputs
    if ((e.altKey || e.ctrlKey || e.metaKey) && handleGlobalShortcuts(e)) return;

    const tag = e.target.tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") {
      if (e.key === "Escape") {
        e.target.blur();
        e.preventDefault();
      }
      return;
    }

    if (isDialogOpen()) return;

    const s = getState();

    // Non-viewer views: Home/End scroll the list
    if (!s.pdfDoc) {
      if (e.key === "Home" || e.key === "End") {
        const wrap = s.currentView === "library"
          ? document.getElementById("library-table-wrap")
          : s.currentView === "setlists"
            ? document.getElementById("setlist-list-wrap")
            : s.currentView === "recent"
              ? document.getElementById("recent-table-wrap")
              : null;
        if (wrap) {
          e.preventDefault();
          wrap.scrollTop = e.key === "Home" ? 0 : wrap.scrollHeight;
        }
      }
      return;
    }

    // Viewer shortcuts
    handleViewerShortcuts(e);
  });
}
