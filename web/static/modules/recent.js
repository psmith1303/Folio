// ---------------------------------------------------------------------------
// Recent files — server-persisted, shared across instances
// ---------------------------------------------------------------------------

import { recentBody, recentStatus } from "./dom.js";
import { api } from "./api.js";
import { esc } from "./utils.js";
import { openScore } from "./viewer.js";
import {
  CACHE_AVAILABLE, isCached, toggleCache, refreshCacheStatus,
  ICON_PINNED, ICON_NOT_CACHED,
} from "./cache.js";


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
  recentBody.innerHTML = "";

  if (list.length === 0) {
    recentStatus.textContent = "No recently viewed scores.";
    return;
  }

  for (const entry of list) {
    const tags = entry.tags || [];
    const tr = document.createElement("tr");
    tr.dataset.filepath = entry.filepath;
    const cached = isCached(entry.filepath);
    tr.innerHTML = `
      <td title="${esc(entry.composer)}">${esc(entry.composer)}</td>
      <td title="${esc(entry.title)}">${esc(entry.title)}</td>
      <td title="${esc(tags.join(", "))}">${esc(tags.join(", "))}</td>
      <td>${formatRelativeTime(entry.timestamp)}</td>
      ${CACHE_AVAILABLE ? `<td class="cache-col"><button class="cache-btn small-btn${cached ? " cached" : ""}" title="${cached ? "Remove from offline cache" : "Download for offline use"}">${cached ? ICON_PINNED : ICON_NOT_CACHED}</button></td>` : ""}
    `;
    tr.addEventListener("click", (e) => {
      if (e.target.closest(".cache-btn")) return;
      openScore({
        filepath: entry.filepath,
        composer: entry.composer,
        title: entry.title,
      });
    });
    const cacheBtn = tr.querySelector(".cache-btn");
    if (cacheBtn) {
      cacheBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleCache(entry.filepath, cacheBtn);
      });
    }
    recentBody.appendChild(tr);
  }
  recentStatus.textContent = `${list.length} recent scores`;
  if (CACHE_AVAILABLE) refreshCacheStatus(recentBody);
}
