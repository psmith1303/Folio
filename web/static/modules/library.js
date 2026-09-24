// ---------------------------------------------------------------------------
// Library — loading, rendering, sorting, filtering
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import {
  searchInput, composerFilter, tagBar, libraryBody, libraryStatus,
  btnReset,
} from "./dom.js";
import { api } from "./api.js";
import { esc } from "./utils.js";
import { filterLibrary } from "./library-filter.js";
import { openScore } from "./viewer.js";
import {
  CACHE_AVAILABLE, isCached, toggleCache, refreshCacheStatus,
  ICON_PINNED, ICON_NOT_CACHED, refreshCachedConfig,
} from "./cache.js";

// ---------------------------------------------------------------------------
// Load and render
// ---------------------------------------------------------------------------

let _loadGen = 0;

// loadLibrary() pulls the whole set once (/api/library is a plain list) and
// every subsequent narrowing runs through applyFilters() below, against the
// in-memory allScores — no network round-trip at all. The setlist song
// picker searches the same in-memory set (searchScores in library-filter.js).
//
// That is what makes filtering work offline. Previously each filter produced
// its own request URL, and the service worker caches API responses under the
// exact URL (handleApiGetFetch in sw.js), so a filtered view existed offline
// only if that precise query string had been fetched while online. Anything
// else got a 503, and loadLibrary's catch returned before rendering — leaving
// the table showing its previous rows, which read as the filter doing nothing.
//
// What this does NOT claim: a cold offline launch additionally depends on
// /api/config being cached, because initApp() awaits it before calling
// loadLibrary() at all (see the cached-GET list in sw.js).
export async function loadLibrary() {
  const gen = ++_loadGen;
  const s = getState();
  try {
    const data = await api("/api/library");
    if (gen !== _loadGen) return;
    s.allScores = data.scores;
    s.libraryLoaded = true;
    applyFilters();
  } catch (err) {
    if (gen !== _loadGen) return;
    libraryStatus.textContent = `Error: ${err.message}`;
  }
}

// ---------------------------------------------------------------------------
// Client-side filtering (rules in library-filter.js)
// ---------------------------------------------------------------------------

function applyFilters() {
  const s = getState();
  const view = filterLibrary(s.allScores, {
    q: searchInput.value,
    composer: composerFilter.value,
    tags: [...s.selectedTags],
    sort: s.sortCol,
    desc: s.sortDesc,
  });
  s.scores = view.scores;
  s.composers = view.composers;
  s.tags = view.tags;

  renderLibrary();
  renderComposerFilter();
  renderTags();
  libraryStatus.textContent = `${view.scores.length} scores`;
  if (CACHE_AVAILABLE) refreshCacheStatus();
}

function renderLibrary() {
  const s = getState();
  libraryBody.innerHTML = "";
  for (const sc of s.scores) {
    const tr = document.createElement("tr");
    tr.dataset.filepath = sc.filepath;
    const cached = isCached(sc.filepath);
    tr.innerHTML = `
      <td title="${esc(sc.composer)}">${esc(sc.composer)}</td>
      <td title="${esc(sc.title)}">${esc(sc.title)}</td>
      <td title="${esc(sc.tags.join(", "))}">${esc(sc.tags.join(", "))}</td>
      ${CACHE_AVAILABLE ? `<td class="cache-col"><button class="cache-btn small-btn${cached ? " cached" : ""}" title="${cached ? "Remove from offline cache" : "Download for offline use"}">${cached ? ICON_PINNED : ICON_NOT_CACHED}</button></td>` : ""}
    `;
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".cache-btn")) return;
      openScore(sc);
    });
    const cacheBtn = tr.querySelector(".cache-btn");
    if (cacheBtn) {
      cacheBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleCache(sc.filepath, cacheBtn);
      });
    }
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
      applyFilters();
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
      applyFilters();
    });
  });

  let searchTimer = null;
  searchInput.addEventListener("input", () => {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(applyFilters, 200);
  });

  composerFilter.addEventListener("change", applyFilters);

  btnReset.addEventListener("click", async () => {
    if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
    const s = getState();
    searchInput.value = "";
    composerFilter.value = "";
    s.selectedTags.clear();
    s.sortCol = "composer";
    s.sortDesc = false;
    updateSortHeaders();

    btnReset.disabled = true;
    const prevLabel = btnReset.textContent;
    btnReset.textContent = "Rescanning…";
    libraryStatus.innerHTML = '<span class="spinner"></span><span>Rescanning library…</span>';

    try {
      await api("/api/library/rescan", { method: "POST" });
      await loadLibrary();
      refreshCachedConfig();
    } catch (err) {
      libraryStatus.textContent = `Rescan failed: ${err.message}`;
    } finally {
      btnReset.disabled = false;
      btnReset.textContent = prevLabel;
    }

    document.getElementById("library-table-wrap").scrollTop = 0;
  });
}
