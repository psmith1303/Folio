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
import { openScore, cleanupScore } from "./viewer.js";
import {
  CACHE_AVAILABLE, isCached, toggleCache, refreshCacheStatus,
  ICON_PINNED, ICON_NOT_CACHED, refreshCachedConfig,
} from "./cache.js";

// ---------------------------------------------------------------------------
// Load and render
// ---------------------------------------------------------------------------

let _loadGen = 0;

// The server can filter, but the library view no longer asks it to:
// loadLibrary() pulls the whole set once and every subsequent narrowing runs
// through applyFilters() below, against the in-memory allScores — no network
// round-trip at all.
//
// That is what makes filtering work offline. Previously each filter produced
// its own request URL, and the service worker caches API responses under the
// exact URL (handleApiGetFetch in sw.js), so a filtered view existed offline
// only if that precise query string had been fetched while online. Anything
// else got a 503, and loadLibrary's catch returned before rendering — leaving
// the table showing its previous rows, which read as the filter doing nothing.
//
// Two things this does NOT claim. A cold offline launch additionally depends
// on /api/config being cached, because initApp() awaits it before calling
// loadLibrary() at all (see the cached-GET list in sw.js). And the setlist
// song picker (renderSongPicker in setlists.js) still filters server-side, so
// it keeps the failure described above; it could filter from allScores too.
export async function loadLibrary() {
  const gen = ++_loadGen;
  const s = getState();
  try {
    const data = await api("/api/library");
    if (gen !== _loadGen) return;
    s.allScores = data.scores;
    applyFilters();
  } catch (err) {
    if (gen !== _loadGen) return;
    libraryStatus.textContent = `Error: ${err.message}`;
  }
}

// ---------------------------------------------------------------------------
// Client-side filtering
//
// Mirrors get_library() in web/server.py: case-insensitive substring on title
// or composer, exact composer match, and all selected tags present. Tag
// matching is an exact subset here — the endpoint lowercases the requested
// tags before comparing them against the score's raw tags, which would drop
// any tag carrying uppercase. Selected tags always come from the server's own
// tag list, so exact matching is both correct and free of that edge.
// ---------------------------------------------------------------------------

const cmpStr = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

// Element-wise, shorter-is-smaller — matches how Python orders tuples/lists.
function cmpArr(a, b) {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const c = cmpStr(a[i], b[i]);
    if (c !== 0) return c;
  }
  return a.length - b.length;
}

const SORTERS = {
  composer: (a, b) => cmpArr(
    [a.composer.toLowerCase(), a.title.toLowerCase()],
    [b.composer.toLowerCase(), b.title.toLowerCase()],
  ),
  title: (a, b) => cmpArr(
    [a.title.toLowerCase(), a.composer.toLowerCase()],
    [b.title.toLowerCase(), b.composer.toLowerCase()],
  ),
  // Title is a third key the endpoint does not have. Sorting by tags leaves
  // same-composer/same-tag scores tied, and their order would then depend on
  // how the fetched list happened to be ordered. Breaking on title makes the
  // ordering total and independent of that.
  tags: (a, b) =>
    cmpArr([...a.tags].sort(cmpStr), [...b.tags].sort(cmpStr)) ||
    cmpStr(a.composer.toLowerCase(), b.composer.toLowerCase()) ||
    cmpStr(a.title.toLowerCase(), b.title.toLowerCase()),
};

function applyFilters() {
  const s = getState();
  const q = searchInput.value.trim().toLowerCase();
  const comp = composerFilter.value;
  const selected = [...s.selectedTags];

  const textMatch = (sc) =>
    !q || sc.title.toLowerCase().includes(q) || sc.composer.toLowerCase().includes(q);
  const compMatch = (sc) => !comp || sc.composer === comp;
  const tagMatch = (sc) => selected.every((t) => sc.tags.includes(t));

  const matches = s.allScores.filter(
    (sc) => textMatch(sc) && compMatch(sc) && tagMatch(sc),
  );
  const sorter = SORTERS[s.sortCol];
  if (sorter) matches.sort(s.sortDesc ? (a, b) => sorter(b, a) : sorter);

  // Facets stay context-sensitive, as the endpoint had them: the composer list
  // ignores the composer filter (so you can still switch to another), the tag
  // list respects it.
  const composers = new Set();
  const tags = new Set();
  for (const sc of s.allScores) {
    if (!textMatch(sc) || !tagMatch(sc)) continue;
    composers.add(sc.composer);
    if (compMatch(sc)) for (const t of sc.tags) tags.add(t);
  }

  s.scores = matches;
  s.composers = [...composers].sort(cmpStr);
  s.tags = [...tags].sort(cmpStr);

  renderLibrary();
  renderComposerFilter();
  renderTags();
  libraryStatus.textContent = `${matches.length} scores`;
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

  btnLibrary.addEventListener("click", () => {
    if (getState().currentView === "viewer") cleanupScore();
    showView("library");
    loadLibrary();
  });
  btnSetlists.addEventListener("click", () => {
    if (getState().currentView === "viewer") cleanupScore();
    showView("setlists");
    if (_loadSetlists) _loadSetlists();
  });
}
