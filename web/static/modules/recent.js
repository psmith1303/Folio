// ---------------------------------------------------------------------------
// Recent files — localStorage persistence, rendering, nav tab handler
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import { recentBody, recentStatus, btnRecent } from "./dom.js";
import { esc } from "./utils.js";
import { showView } from "./views.js";

const STORAGE_KEY = "folio-recent";
const MAX_RECENT = 50;

// Callbacks set by app.js to avoid circular deps (viewer <-> recent)
let _openScore = null;
let _cleanupScore = null;
export function setRecentCallbacks(openFn, cleanupFn) {
  _openScore = openFn;
  _cleanupScore = cleanupFn;
}

// ---------------------------------------------------------------------------
// localStorage helpers
// ---------------------------------------------------------------------------

function loadRecentList() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || [];
  } catch {
    return [];
  }
}

function saveRecentList(list) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
}

export function addToRecent(score) {
  const list = loadRecentList();
  const filtered = list.filter((e) => e.filepath !== score.filepath);
  filtered.unshift({
    filepath: score.filepath,
    composer: score.composer,
    title: score.title,
    timestamp: Date.now(),
  });
  if (filtered.length > MAX_RECENT) filtered.length = MAX_RECENT;
  saveRecentList(filtered);
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

export function renderRecent() {
  const list = loadRecentList();
  recentBody.innerHTML = "";

  if (list.length === 0) {
    recentStatus.textContent = "No recently viewed scores.";
    return;
  }

  for (const entry of list) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td title="${esc(entry.composer)}">${esc(entry.composer)}</td>
      <td title="${esc(entry.title)}">${esc(entry.title)}</td>
      <td>${formatRelativeTime(entry.timestamp)}</td>
    `;
    tr.addEventListener("click", () => {
      if (_openScore) {
        _openScore({
          filepath: entry.filepath,
          composer: entry.composer,
          title: entry.title,
        });
      }
    });
    recentBody.appendChild(tr);
  }
  recentStatus.textContent = `${list.length} recent scores`;
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

export function initRecentEvents() {
  btnRecent.addEventListener("click", () => {
    if (getState().currentView === "viewer" && _cleanupScore) _cleanupScore();
    showView("recent");
    renderRecent();
  });
}
