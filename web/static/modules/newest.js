// ---------------------------------------------------------------------------
// Newest files — the most recently added/modified PDFs in the library
// ---------------------------------------------------------------------------

import { newestBody, newestStatus } from "./dom.js";
import { api } from "./api.js";
import { renderScoreRows } from "./score-table.js";
import { CACHE_AVAILABLE, refreshCacheStatus } from "./cache.js";


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
  // Rendered even when empty, to clear the previous rows.
  renderScoreRows(newestBody, list, { extra: (sc) => formatAdded(sc.mtime) });

  if (list.length === 0) {
    newestStatus.textContent = "No scores in library.";
    return;
  }
  newestStatus.textContent = `${list.length} newest scores`;
  if (CACHE_AVAILABLE) refreshCacheStatus(newestBody);
}
