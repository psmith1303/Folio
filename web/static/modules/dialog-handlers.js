// ---------------------------------------------------------------------------
// Dialog handlers — per-dialog show/close logic
// Integration layer: reaches across domain modules.
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import {
  dirDialog, dirInput, dirCancel,
  textDialog, textDialogTitle, textInput, textFont, textCancel,
  textSizeSlider, textSizePt,
  clearPageDialog, clearPageCancel, btnClearPage,
  conflictDialog, conflictReload, conflictForce,
  btnSetDir, btnAddToSetlist, btnEditTags,
  setlistPickerDialog, setlistPickerList, setlistPickerCancel,
  setlistPickerStart, setlistPickerEnd, setlistPickerAdd,
  tagEditorDialog, tagEditorChips, tagEditorInput, tagEditorAddBtn,
  tagEditorCancel, titleDisplay,
  libraryStatus,
} from "./dom.js";
import { api } from "./api.js";
import { refreshCachedConfig } from "./cache.js";
import { esc, sizeToPt } from "./utils.js";
import {
  saveAnnotations,
  commitTextAnnotation, cancelTextAnnotation,
  clearCurrentPageAnnotations,
} from "./annotations.js";
import { renderPage } from "./viewer.js";
import { loadLibrary } from "./library.js";
import { addCurrentScoreToSetlist } from "./setlists.js";

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
      // The old library's scores are no longer valid: drop them, so a failed
      // reload leaves the song picker saying "not loaded" rather than
      // offering songs from the previous library.
      const s = getState();
      s.selectedTags.clear();
      s.allScores = [];
      s.libraryLoaded = false;
      await loadLibrary();
      refreshCachedConfig();
    } catch (err) {
      libraryStatus.textContent = `Error: ${err.message}`;
    }
  });
}

// ---------------------------------------------------------------------------
// Text annotation dialog
// ---------------------------------------------------------------------------

function updateTextSizePt() {
  textSizePt.textContent = `${sizeToPt(parseInt(textSizeSlider.value, 10))}pt`;
}

// Opened by the text tool. The dialog's own size slider is independent of
// the shared pen/stamp toolbar slider (it has a wider range) and, for new
// annotations, remembers the last size used across dialog opens.
export function showTextDialog(editAnnot) {
  if (editAnnot) {
    textDialogTitle.textContent = "Edit Text";
    textInput.value = editAnnot.text;
    textFont.value = editAnnot.font || "sans-serif";
    textSizeSlider.value = editAnnot.size || textSizeSlider.value;
  } else {
    textDialogTitle.textContent = "Add Text";
    textInput.value = "";
    textFont.value = "sans-serif";
  }
  updateTextSizePt();
  textDialog.showModal();
  textInput.focus();
}

function initTextDialog() {
  textSizeSlider.addEventListener("input", updateTextSizePt);

  // Ctrl/Cmd+Enter submits the textarea (plain Enter inserts a newline)
  textInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      textDialog.querySelector("#text-ok").click();
    }
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
    commitTextAnnotation(text, textFont.value, parseInt(textSizeSlider.value, 10));
  });
}

// ---------------------------------------------------------------------------
// Clear-page confirmation dialog
// ---------------------------------------------------------------------------

function initClearPageDialog() {
  btnClearPage.addEventListener("click", () => {
    const s = getState();
    if (!s.pdfDoc) return;
    const pg = String(s.currentPage - 1);
    const pageAnnots = s.annotations[pg];
    if (!pageAnnots || pageAnnots.length === 0) return;
    clearPageDialog.showModal();
  });

  clearPageCancel.addEventListener("click", () => clearPageDialog.close());

  clearPageDialog.addEventListener("close", () => {
    if (clearPageDialog.returnValue !== "clear") return;
    clearCurrentPageAnnotations();
  });
}

// ---------------------------------------------------------------------------
// Conflict dialog
// ---------------------------------------------------------------------------

// Opened when an annotation save hits a newer version on the server.
export function showConflictDialog() {
  conflictDialog.showModal();
}

function initConflictDialog() {
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

let _tagEditorLoading = false;

export async function showTagEditor() {
  const s = getState();
  if (!s.currentScore) return;
  // A second open while the first fetch is in flight would throw from
  // showModal() and clobber chips the user already added.
  if (tagEditorDialog.open || _tagEditorLoading) return;

  // currentScore may be a partial record -- scores opened from Recent or a
  // setlist carry only filepath/composer/title -- so fetch the authoritative
  // tags rather than seeding an empty list and saving that back.
  let score;
  const requestedPath = s.currentScore.filepath;
  _tagEditorLoading = true;
  try {
    score = await api(
      `/api/scores?path=${encodeURIComponent(requestedPath)}`);
  } catch (err) {
    console.error("Failed to load tags:", err);
    alert("Could not load this score's tags. Check the connection to Folio "
          + "and try again.");
    return;
  } finally {
    _tagEditorLoading = false;
  }

  // The viewer can move on while the fetch is in flight, so bail rather than
  // opening the editor seeded with the previous song's tags.
  if (!s.currentScore || s.currentScore.filepath !== requestedPath) return;

  s._editingFolderTags = score.folder_tags || [];
  s._editingFilenameTags = [...(score.filename_tags || [])];
  s._tagEditorPath = requestedPath;
  s._tagEditorLoaded = true;
  tagEditorInput.value = "";
  renderTagEditorChips();
  // Not every browser clears returnValue on open, so a previous "save" can
  // otherwise make the next Cancel behave like a save.
  tagEditorDialog.returnValue = "";
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
    const s = getState();
    const loaded = s._tagEditorLoaded;
    const editedPath = s._tagEditorPath;
    s._tagEditorLoaded = false;
    s._tagEditorPath = "";
    if (tagEditorDialog.returnValue !== "save") return;
    // Never write back tags the editor did not successfully load: an empty
    // list must mean the user removed every chip.
    if (!loaded) return;
    // The viewer can move to a different score while the editor is open --
    // a page turn at a setlist boundary reassigns currentScore -- so never
    // rename whatever happens to be on screen now with the loaded tags.
    if (!s.currentScore || s.currentScore.filepath !== editedPath) {
      alert("The open score changed while the tag editor was open, so the "
            + "tags were not saved.");
      return;
    }
    _tagEditorLoading = true;
    try {
      const data = await api("/api/scores/tags", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: editedPath,
          filename_tags: s._editingFilenameTags,
        }),
      });
      // The file has been renamed on disk, so the one-shot setlist playback
      // snapshot is now stale regardless of what the viewer shows -- leaving
      // it makes the song unloadable when the user comes back to it.
      const pb = s.setlistPlayback;
      if (pb) {
        for (const song of pb.songs) {
          if (song.path === editedPath) song.path = data.score.filepath;
        }
      }
      // Likewise the loaded library, which the song picker searches: the
      // score may have been opened from Recent or a setlist (a separate
      // copy), so its row there would keep offering the old path.
      const i = s.allScores.findIndex((sc) => sc.filepath === editedPath);
      if (i !== -1) s.allScores[i] = data.score;
      // The viewer may have moved to another score while the PUT was in
      // flight; stamping this record onto that one would misdirect annotation
      // saves.
      if (!s.currentScore || s.currentScore.filepath !== editedPath) return;
      s.currentScore.filepath = data.score.filepath;
      s.currentScore.filename = data.score.filename;
      s.currentScore.tags = data.score.tags;
      s.currentScore.folder_tags = data.score.folder_tags;
      s.currentScore.filename_tags = data.score.filename_tags;
      titleDisplay.textContent = `${s.currentScore.composer} \u2014 ${s.currentScore.title}`;
    } catch (err) {
      console.error("Failed to update tags:", err);
      alert("Failed to update tags: " + err.message);
    } finally {
      _tagEditorLoading = false;
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
  initClearPageDialog();
  initConflictDialog();
  initSetlistPickerDialog();
  initTagEditorDialog();
}
