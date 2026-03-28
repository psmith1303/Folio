// ---------------------------------------------------------------------------
// Library — loading, rendering, sorting, filtering
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import {
  searchInput, composerFilter, tagBar, libraryBody, libraryStatus,
  btnReset, btnLibrary, btnSetlists,
} from "./dom.js";
import { api } from "./api.js";
import { esc } from "./utils.js";
import { showView } from "./views.js";
import { openScore } from "./viewer.js";

// ---------------------------------------------------------------------------
// Load and render
// ---------------------------------------------------------------------------

export async function loadLibrary() {
  const s = getState();
  const params = new URLSearchParams();
  const q = searchInput.value.trim();
  if (q) params.set("q", q);
  const comp = composerFilter.value;
  if (comp) params.set("composer", comp);
  for (const t of s.selectedTags) {
    params.append("tag", t);
  }
  params.set("sort", s.sortCol);
  if (s.sortDesc) params.set("desc", "true");

  try {
    const data = await api(`/api/library?${params}`);
    s.scores = data.scores;
    s.composers = data.composers;
    s.tags = data.tags;
    renderLibrary();
    renderComposerFilter();
    renderTags();
    libraryStatus.textContent = `${data.total} scores`;
  } catch (err) {
    libraryStatus.textContent = `Error: ${err.message}`;
  }
}

function renderLibrary() {
  const s = getState();
  libraryBody.innerHTML = "";
  for (const sc of s.scores) {
    const tr = document.createElement("tr");
    tr.dataset.filepath = sc.filepath;
    tr.innerHTML = `
      <td title="${esc(sc.composer)}">${esc(sc.composer)}</td>
      <td title="${esc(sc.title)}">${esc(sc.title)}</td>
      <td title="${esc(sc.tags.join(", "))}">${esc(sc.tags.join(", "))}</td>
    `;
    tr.addEventListener("click", () => openScore(sc));
    libraryBody.appendChild(tr);
  }
}

function renderComposerFilter() {
  const s = getState();
  const current = composerFilter.value;
  composerFilter.innerHTML = '<option value="">All Composers</option>';
  for (const c of s.composers) {
    const opt = document.createElement("option");
    opt.value = c;
    opt.textContent = c;
    if (c === current) opt.selected = true;
    composerFilter.appendChild(opt);
  }
}

function renderTags() {
  const s = getState();
  tagBar.innerHTML = "";
  for (const t of s.tags) {
    const chip = document.createElement("span");
    chip.className = "tag-chip" + (s.selectedTags.has(t) ? " selected" : "");
    chip.textContent = t;
    chip.addEventListener("click", () => {
      if (s.selectedTags.has(t)) {
        s.selectedTags.delete(t);
      } else {
        s.selectedTags.add(t);
      }
      loadLibrary();
    });
    tagBar.appendChild(chip);
  }
}

// ---------------------------------------------------------------------------
// Sorting
// ---------------------------------------------------------------------------

function updateSortHeaders() {
  const s = getState();
  document.querySelectorAll("th.sortable").forEach((th) => {
    th.classList.remove("sort-asc", "sort-desc");
    const col = th.dataset.col;
    const base = col.charAt(0).toUpperCase() + col.slice(1);
    if (col === s.sortCol) {
      th.classList.add(s.sortDesc ? "sort-desc" : "sort-asc");
      th.textContent = base + (s.sortDesc ? " \u25BC" : " \u25B2");
    } else {
      th.textContent = base;
    }
  });
}

// ---------------------------------------------------------------------------
// Init event listeners
// ---------------------------------------------------------------------------

// Lazy import to break circular dependency (setlists imports viewer -> library)
let _loadSetlists = null;
export function setLoadSetlistsFn(fn) { _loadSetlists = fn; }

export function initLibraryEvents() {
  document.querySelectorAll("th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const s = getState();
      const col = th.dataset.col;
      if (s.sortCol === col) {
        s.sortDesc = !s.sortDesc;
      } else {
        s.sortCol = col;
        s.sortDesc = false;
      }
      updateSortHeaders();
      loadLibrary();
    });
  });

  let searchTimer = null;
  searchInput.addEventListener("input", () => {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(loadLibrary, 200);
  });

  composerFilter.addEventListener("change", loadLibrary);

  btnReset.addEventListener("click", async () => {
    const s = getState();
    searchInput.value = "";
    composerFilter.value = "";
    s.selectedTags.clear();
    s.sortCol = "composer";
    s.sortDesc = false;
    updateSortHeaders();
    try {
      await api("/api/library/rescan", { method: "POST" });
    } catch (err) {
      console.error("Rescan failed:", err);
    }
    if ("caches" in window) {
      const names = await caches.keys();
      await Promise.all(names.map((n) => caches.delete(n)));
    }
    loadLibrary();
  });

  btnLibrary.addEventListener("click", () => { showView("library"); loadLibrary(); });
  btnSetlists.addEventListener("click", () => {
    showView("setlists");
    if (_loadSetlists) _loadSetlists();
  });
}
