// ---------------------------------------------------------------------------
// Keyboard shortcuts — configurable, data-driven keydown handler
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import { btnReset, searchInput } from "./dom.js";
import { setTool, doUndo, isPlacementTool, rotatePage } from "./annotations.js";
import {
  nextPage, prevPage, goToPage, closeScore,
  toggleFullscreen, applyFullscreen,
} from "./viewer.js";
import { showSetlistPicker, showTagEditor } from "./dialog-handlers.js";
import { navigate, listScroller } from "./views.js";

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

let _parsed = {};

export function setKeybindings(bindings) {
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

// While any dialog is open the page underneath takes no shortcuts. Asking
// the page, rather than keeping a list, means a new dialog can't be missed
// (the clear-page confirmation was, so keys turned pages behind it).
function isDialogOpen() {
  return document.querySelector("dialog[open]") !== null;
}

// Alt+/Ctrl+ combos, which work from any view and even from input fields
// (they don't conflict with typing). All suppress the browser default.
const GLOBAL_ACTIONS = {
  go_library: () => navigate("library"),
  go_setlists: () => navigate("setlists"),
  go_recent: () => navigate("recent"),
  go_newest: () => navigate("newest"),
  focus_search: () => { searchInput.focus(); searchInput.select(); },
  reset_filters: () => btnReset.click(),
};

function handleGlobalShortcuts(e) {
  for (const [action, run] of Object.entries(GLOBAL_ACTIONS)) {
    if (matches(e, action)) {
      e.preventDefault();
      run();
      return true;
    }
  }
  return false;
}

// Viewer actions, checked in order before page navigation. Only undo
// suppresses the browser default (Ctrl+Z).
const VIEWER_ACTIONS = [
  ["tool_nav", () => setTool("nav")],
  ["tool_pen", () => setTool("pen")],
  ["tool_text", () => setTool("text")],
  ["tool_eraser", () => setTool("eraser")],
  ["tool_move", () => setTool("move")],
  ["toggle_fullscreen", toggleFullscreen],
  ["add_to_setlist", showSetlistPicker],
  ["edit_tags", showTagEditor],
  ["rotate_cw", () => rotatePage(90)],
  ["rotate_ccw", () => rotatePage(-90)],
  ["undo", doUndo, { preventDefault: true }],
];

function handleViewerShortcuts(e) {
  const s = getState();

  for (const [action, run, opts] of VIEWER_ACTIONS) {
    if (matches(e, action)) {
      if (opts?.preventDefault) e.preventDefault();
      run();
      return true;
    }
  }

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
    // Escape first cancels a placement tool, before closing the score.
    if (isPlacementTool(s.activeTool)) { setTool("nav"); return true; }
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
        const id = listScroller(s.currentView);
        const wrap = id ? document.getElementById(id) : null;
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
