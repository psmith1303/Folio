// ---------------------------------------------------------------------------
// Recent files — server-persisted, shared across instances
// ---------------------------------------------------------------------------

import { recentBody, recentStatus } from "./dom.js";
import { api } from "./api.js";
import { renderScoreRows } from "./score-table.js";
import { CACHE_AVAILABLE, refreshCacheStatus } from "./cache.js";


// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function fetchRecent() {
  try {
    const data = await api("/api/recent");
    return data.recent || [];
  } catch {
    return [];
  }
}

export async function addToRecent(score) {
  if (!score || !score.filepath) return;
  try {
    await api("/api/recent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: score.filepath }),
    });
  } catch (err) {
    console.error("Failed to record recent:", err);
  }
}

// ---------------------------------------------------------------------------
// Relative time formatting
// ---------------------------------------------------------------------------

function formatRelativeTime(ts) {
  const diff = Date.now() - ts;
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  const d = new Date(ts);
  const month = d.toLocaleString("default", { month: "short" });
  return `${month} ${d.getDate()}`;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

export async function renderRecent() {
  const list = await fetchRecent();
  // Rendered even when empty, to clear the previous rows. A recent entry
  // opens as a partial record: its stored tags may be stale.
  renderScoreRows(recentBody, list, {
    extra: (entry) => formatRelativeTime(entry.timestamp),
    toOpen: ({ filepath, composer, title }) => ({ filepath, composer, title }),
  });

  if (list.length === 0) {
    recentStatus.textContent = "No recently viewed scores.";
    return;
  }
  recentStatus.textContent = `${list.length} recent scores`;
  if (CACHE_AVAILABLE) refreshCacheStatus(recentBody);
}
