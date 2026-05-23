// ---------------------------------------------------------------------------
// Newest files — the most recently added/modified PDFs in the library
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import { newestBody, newestStatus, btnNewest } from "./dom.js";
import { api } from "./api.js";
import { esc } from "./utils.js";
import { showView } from "./views.js";
import {
  CACHE_AVAILABLE, isCached, toggleCache, refreshCacheStatus,
  ICON_PINNED, ICON_NOT_CACHED,
} from "./cache.js";

// Callbacks set by app.js to avoid circular deps (viewer <-> newest)
let _openScore = null;
let _cleanupScore = null;
export function setNewestCallbacks(openFn, cleanupFn) {
  _openScore = openFn;
  _cleanupScore = cleanupFn;
}

const NEWEST_LIMIT = 20;

async function fetchNewest() {
  try {
    const data = await api(`/api/newest?limit=${NEWEST_LIMIT}`);
    return data.scores || [];
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------------------
// Relative date formatting (mtime is a Unix timestamp in seconds)
// ---------------------------------------------------------------------------

function formatAdded(mtimeSec) {
  if (!mtimeSec) return "Unknown";
  const diff = Date.now() - mtimeSec * 1000;
  const days = Math.floor(diff / 86400000);
  if (days < 1) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  const d = new Date(mtimeSec * 1000);
  const month = d.toLocaleString("default", { month: "short" });
  return `${month} ${d.getDate()}, ${d.getFullYear()}`;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

export async function renderNewest() {
  const list = await fetchNewest();
  newestBody.innerHTML = "";

  if (list.length === 0) {
    newestStatus.textContent = "No scores in library.";
    return;
  }

  for (const sc of list) {
    const tr = document.createElement("tr");
    tr.dataset.filepath = sc.filepath;
    const cached = isCached(sc.filepath);
    tr.innerHTML = `
      <td title="${esc(sc.composer)}">${esc(sc.composer)}</td>
      <td title="${esc(sc.title)}">${esc(sc.title)}</td>
      <td title="${esc(sc.tags.join(", "))}">${esc(sc.tags.join(", "))}</td>
      <td>${formatAdded(sc.mtime)}</td>
      ${CACHE_AVAILABLE ? `<td class="cache-col"><button class="cache-btn small-btn${cached ? " cached" : ""}" title="${cached ? "Remove from offline cache" : "Download for offline use"}">${cached ? ICON_PINNED : ICON_NOT_CACHED}</button></td>` : ""}
    `;
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".cache-btn")) return;
      if (_openScore) _openScore(sc);
    });
    const cacheBtn = tr.querySelector(".cache-btn");
    if (cacheBtn) {
      cacheBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleCache(sc.filepath, cacheBtn);
      });
    }
    newestBody.appendChild(tr);
  }
  newestStatus.textContent = `${list.length} newest scores`;
  if (CACHE_AVAILABLE) refreshCacheStatus(newestBody);
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

export function initNewestEvents() {
  btnNewest.addEventListener("click", () => {
    if (getState().currentView === "viewer" && _cleanupScore) _cleanupScore();
    showView("newest");
    renderNewest();
  });
}
