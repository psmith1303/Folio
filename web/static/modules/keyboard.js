// ---------------------------------------------------------------------------
// Keyboard shortcuts — single global keydown handler
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import {
  textDialog, dirDialog, setlistNameDialog, songPickerDialog,
  setlistPickerDialog, setlistRefPickerDialog, loginDialog,
  conflictDialog, offlineDialog,
} from "./dom.js";
import { setTool, doUndo } from "./annotations.js";
import {
  nextPage, prevPage, goToPage, closeScore,
  toggleFullscreen, applyFullscreen,
} from "./viewer.js";
import { showSetlistPicker, showTagEditor } from "./dialog-handlers.js";
import { rotatePage } from "./annotations.js";

export function initKeyboardShortcuts() {
  document.addEventListener("keydown", (e) => {
    const tag = e.target.tagName;
    if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") {
      if (e.key === "Escape") {
        e.target.blur();
        e.preventDefault();
      }
      return;
    }

    // Dialog open — don't handle
    if (
      textDialog.open || dirDialog.open || setlistNameDialog.open ||
      songPickerDialog.open || setlistPickerDialog.open ||
      setlistRefPickerDialog.open || loginDialog.open || conflictDialog.open ||
      offlineDialog.open
    ) return;

    const s = getState();

    // Library/setlist view: Home/End scroll the list
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

    // Tool shortcuts
    switch (e.key) {
      case "v": setTool("nav"); return;
      case "d": setTool("pen"); return;
      case "t": setTool("text"); return;
      case "e": setTool("eraser"); return;
      case "f": toggleFullscreen(); return;
      case "s": showSetlistPicker(); return;
      case "g": showTagEditor(); return;
      case "r": rotatePage(90); return;
      case "R": rotatePage(-90); return;
    }

    // Undo
    if (e.key === "z" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      doUndo();
      return;
    }

    // In wide mode, let arrow up/down and space scroll natively
    if (s.displayMode === "wide" && (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === " ")) {
      return;
    }

    switch (e.key) {
      case "ArrowRight":
      case "ArrowDown":
      case " ":
      case "n":
      case "PageDown":
        e.preventDefault();
        nextPage();
        break;
      case "ArrowLeft":
      case "ArrowUp":
      case "Backspace":
      case "p":
      case "PageUp":
        e.preventDefault();
        prevPage();
        break;
      case "Home":
        e.preventDefault();
        goToPage(1);
        break;
      case "End":
        e.preventDefault();
        goToPage(s.totalPages);
        break;
      case "Escape":
        if (s.pseudoFullscreen) { applyFullscreen(false); }
        else { closeScore(); }
        break;
    }
  });
}
