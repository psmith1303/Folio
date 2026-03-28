// ---------------------------------------------------------------------------
// Dialog handlers — per-dialog show/close logic
// Integration layer: reaches across domain modules.
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import {
  dirDialog, dirInput, dirCancel,
  textDialog, textDialogTitle, textInput, textFont, textCancel,
  conflictDialog, conflictReload, conflictForce,
  loginDialog, loginInput, loginError,
  btnSetDir, btnAddToSetlist, btnEditTags,
  setlistPickerDialog, setlistPickerList, setlistPickerCancel,
  setlistPickerStart, setlistPickerEnd, setlistPickerAdd,
  tagEditorDialog, tagEditorChips, tagEditorInput, tagEditorAddBtn,
  tagEditorCancel, titleDisplay,
  libraryStatus,
} from "./dom.js";
import { api, login, setLoginHandler } from "./api.js";
import { esc } from "./utils.js";
import {
  saveAnnotations, drawAnnotations,
  setConflictHandler, setTextDialogHandler,
  commitTextAnnotation, cancelTextAnnotation,
} from "./annotations.js";
import { renderPage } from "./viewer.js";

// These are set lazily to avoid circular imports at module evaluation time
let _loadLibrary = null;
let _initApp = null;

export function setLoadLibraryFn(fn) { _loadLibrary = fn; }
export function setInitAppFn(fn) { _initApp = fn; }

// ---------------------------------------------------------------------------
// Set-folder dialog
// ---------------------------------------------------------------------------

export function showDirDialog(defaultPath) {
  dirInput.value = defaultPath || "";
  dirDialog.showModal();
  dirInput.focus();
}

function initDirDialog() {
  btnSetDir.addEventListener("click", () => {
    dirInput.value = "";
    dirDialog.showModal();
    dirInput.focus();
  });

  dirCancel.addEventListener("click", () => dirDialog.close());

  dirDialog.addEventListener("close", async () => {
    const path = dirInput.value.trim();
    if (!path) return;
    try {
      libraryStatus.textContent = "Scanning\u2026";
      await api("/api/library", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      getState().selectedTags.clear();
      if (_loadLibrary) await _loadLibrary();
    } catch (err) {
      libraryStatus.textContent = `Error: ${err.message}`;
    }
  });
}

// ---------------------------------------------------------------------------
// Text annotation dialog
// ---------------------------------------------------------------------------

function initTextDialog() {
  // Register callback so annotations module can open this dialog
  setTextDialogHandler((editAnnot) => {
    const s = getState();
    if (editAnnot) {
      textDialogTitle.textContent = "Edit Text";
      textInput.value = editAnnot.text;
      textFont.value = editAnnot.font || "sans-serif";
    } else {
      textDialogTitle.textContent = "Add Text";
      textInput.value = "";
      textFont.value = "sans-serif";
    }
    textDialog.showModal();
    textInput.focus();
  });

  // Symbol buttons insert into text input
  document.querySelectorAll(".sym-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sym = btn.dataset.sym;
      const pos = textInput.selectionStart;
      textInput.value = textInput.value.slice(0, pos) + sym + textInput.value.slice(pos);
      textInput.focus();
      textInput.setSelectionRange(pos + sym.length, pos + sym.length);
    });
  });

  textCancel.addEventListener("click", () => {
    cancelTextAnnotation();
    textDialog.close();
  });

  textDialog.addEventListener("close", () => {
    const s = getState();
    if (!s.pendingTextAnnot) return;
    const text = textInput.value.trim();
    if (!text) {
      cancelTextAnnotation();
      return;
    }
    commitTextAnnotation(text, textFont.value);
  });
}

// ---------------------------------------------------------------------------
// Conflict dialog
// ---------------------------------------------------------------------------

function initConflictDialog() {
  setConflictHandler(() => conflictDialog.showModal());

  conflictReload.addEventListener("click", async () => {
    conflictDialog.close();
    const s = getState();
    if (!s.currentScore) return;
    try {
      const data = await api(`/api/annotations?path=${encodeURIComponent(s.currentScore.filepath)}`);
      s.annotations = data.pages || {};
      s.rotations = data.rotations || {};
      s.annotationEtag = data.etag || null;
      s.undoStacks = {};
      renderPage();
    } catch (err) {
      console.error("Failed to reload annotations:", err);
    }
  });

  conflictForce.addEventListener("click", () => {
    conflictDialog.close();
    saveAnnotations(true);
  });
}

// ---------------------------------------------------------------------------
// Login dialog
// ---------------------------------------------------------------------------

export function showLoginDialog() {
  loginInput.value = "";
  loginError.textContent = "";
  loginDialog.showModal();
  loginInput.focus();
}

function initLoginDialog() {
  setLoginHandler(showLoginDialog);

  loginDialog.addEventListener("close", async () => {
    if (loginDialog.returnValue !== "login") return;
    const passphrase = loginInput.value.trim();
    if (!passphrase) {
      showLoginDialog();
      return;
    }
    try {
      await login(passphrase);
      if (_initApp) _initApp();
    } catch (err) {
      loginError.textContent = err.message || "Login failed";
      loginDialog.showModal();
      loginInput.focus();
    }
  });
}

// ---------------------------------------------------------------------------
// Setlist picker (add current score to setlist — from viewer)
// ---------------------------------------------------------------------------

function initSetlistPickerDialog() {
  btnAddToSetlist.addEventListener("click", showSetlistPicker);
  setlistPickerCancel.addEventListener("click", () => setlistPickerDialog.close());

  setlistPickerDialog.addEventListener("close", async () => {
    const s = getState();
    if (setlistPickerDialog.returnValue !== "add" || !s._pickerSelectedSetlist) return;
    const startPage = parseInt(setlistPickerStart.value, 10) || 1;
    const endRaw = parseInt(setlistPickerEnd.value, 10) || 0;
    const endPage = endRaw === 0 ? null : endRaw;

    // Lazy import to avoid circular dep
    const { addCurrentScoreToSetlist } = await import("./setlists.js");
    await addCurrentScoreToSetlist(s._pickerSelectedSetlist, startPage, endPage);
  });
}

export async function showSetlistPicker() {
  const s = getState();
  if (!s.currentScore) return;
  s._pickerSelectedSetlist = null;
  setlistPickerAdd.disabled = true;
  setlistPickerStart.value = s.currentPage;
  setlistPickerEnd.value = 0;
  try {
    const data = await api("/api/setlists");
    setlistPickerList.innerHTML = "";
    if (data.setlists.length === 0) {
      setlistPickerList.innerHTML =
        '<p style="padding:10px;color:var(--fg-dim)">No setlists yet. Create one in the Setlists view.</p>';
    } else {
      for (const sl of data.setlists) {
        const div = document.createElement("div");
        div.className = "picker-item";
        div.textContent = `${sl.name} (${sl.count})`;
        div.addEventListener("click", () => {
          setlistPickerList.querySelectorAll(".picker-item").forEach(
            (el) => el.classList.remove("selected")
          );
          div.classList.add("selected");
          s._pickerSelectedSetlist = sl.name;
          setlistPickerAdd.disabled = false;
        });
        setlistPickerList.appendChild(div);
      }
    }
    setlistPickerDialog.showModal();
  } catch (err) {
    console.error("Failed to load setlists:", err);
  }
}

// ---------------------------------------------------------------------------
// Tag editor dialog (from viewer)
// ---------------------------------------------------------------------------

function renderTagEditorChips() {
  const s = getState();
  tagEditorChips.innerHTML = "";
  for (const t of s._editingFolderTags) {
    const span = document.createElement("span");
    span.className = "tag-chip-edit folder";
    span.textContent = t;
    span.title = "Folder tag (not editable)";
    tagEditorChips.appendChild(span);
  }
  for (const t of s._editingFilenameTags) {
    const span = document.createElement("span");
    span.className = "tag-chip-edit";
    span.innerHTML = `${esc(t)}<span class="tag-remove" title="Remove">&times;</span>`;
    span.querySelector(".tag-remove").addEventListener("click", () => {
      s._editingFilenameTags = s._editingFilenameTags.filter((x) => x !== t);
      renderTagEditorChips();
    });
    tagEditorChips.appendChild(span);
  }
}

export function showTagEditor() {
  const s = getState();
  if (!s.currentScore) return;
  s._editingFolderTags = s.currentScore.folder_tags || [];
  s._editingFilenameTags = [...(s.currentScore.filename_tags || [])];
  tagEditorInput.value = "";
  renderTagEditorChips();
  tagEditorDialog.showModal();
}

function initTagEditorDialog() {
  btnEditTags.addEventListener("click", showTagEditor);

  tagEditorAddBtn.addEventListener("click", () => {
    const s = getState();
    const raw = tagEditorInput.value.trim().toLowerCase().replace(/[^\w-]/g, "");
    if (!raw) return;
    if (!s._editingFilenameTags.includes(raw) && !s._editingFolderTags.includes(raw)) {
      s._editingFilenameTags.push(raw);
      renderTagEditorChips();
    }
    tagEditorInput.value = "";
  });

  tagEditorInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      tagEditorAddBtn.click();
    }
  });

  tagEditorDialog.addEventListener("close", async () => {
    if (tagEditorDialog.returnValue !== "save") return;
    const s = getState();
    try {
      const data = await api("/api/scores/tags", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: s.currentScore.filepath,
          filename_tags: s._editingFilenameTags,
        }),
      });
      s.currentScore.filepath = data.score.filepath;
      s.currentScore.filename = data.score.filename;
      s.currentScore.tags = data.score.tags;
      s.currentScore.folder_tags = data.score.folder_tags;
      s.currentScore.filename_tags = data.score.filename_tags;
      titleDisplay.textContent = `${s.currentScore.composer} \u2014 ${s.currentScore.title}`;
    } catch (err) {
      console.error("Failed to update tags:", err);
      alert("Failed to update tags: " + err.message);
    }
  });

  tagEditorCancel.addEventListener("click", () => tagEditorDialog.close());
}

// ---------------------------------------------------------------------------
// Init all dialogs
// ---------------------------------------------------------------------------

export function initDialogHandlers() {
  initDirDialog();
  initTextDialog();
  initConflictDialog();
  initLoginDialog();
  initSetlistPickerDialog();
  initTagEditorDialog();
}
