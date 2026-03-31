// ---------------------------------------------------------------------------
// Offline cache management — MessageChannel communication with service worker
// ---------------------------------------------------------------------------

import { getState } from "./state.js";
import { libraryBody, libraryStatus } from "./dom.js";

const MAX_AUTO_CACHED = 30;

// ---------------------------------------------------------------------------
// SW communication
// ---------------------------------------------------------------------------

async function swMessage(type, payload = {}) {
  if (!navigator.serviceWorker) {
    throw new Error("Service workers not supported");
  }
  const reg = await navigator.serviceWorker.ready;
  const sw = reg.active;
  if (!sw) {
    throw new Error("No active service worker");
  }
  return new Promise((resolve, reject) => {
    const ch = new MessageChannel();
    ch.port1.onmessage = (e) => resolve(e.data);
    sw.postMessage({ type, payload }, [ch.port2]);
    setTimeout(() => reject(new Error("SW message timeout")), 30000);
  });
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function cachePdf(path) {
  return swMessage("cache-pdf", { path });
}

export async function evictPdf(path) {
  return swMessage("evict-pdf", { path });
}

export async function getCacheStatus() {
  try {
    const result = await swMessage("get-cache-status");
    return {
      cached: new Set(result.cachedPaths || []),
      pinned: new Set(result.pinnedPaths || []),
    };
  } catch {
    return { cached: new Set(), pinned: new Set() };
  }
}

export async function clearPdfCache() {
  return swMessage("clear-pdf-cache");
}

export async function cacheLibrary() {
  return swMessage("cache-library");
}

// ---------------------------------------------------------------------------
// UI — per-row cache buttons in library table
// ---------------------------------------------------------------------------

let _cachedPaths = new Set();
let _pinnedPaths = new Set();

export async function refreshCacheStatus() {
  const status = await getCacheStatus();
  _cachedPaths = status.cached;
  _pinnedPaths = status.pinned;
  updateCacheButtons();
}

function updateCacheButtons() {
  const rows = libraryBody.querySelectorAll("tr");
  for (const row of rows) {
    const filepath = row.dataset.filepath;
    if (!filepath) continue;
    const btn = row.querySelector(".cache-btn");
    if (!btn) continue;
    applyCacheButtonState(btn, filepath);
  }
}

function applyCacheButtonState(btn, filepath) {
  const cached = _cachedPaths.has(filepath);
  const pinned = _pinnedPaths.has(filepath);
  if (pinned) {
    btn.textContent = "\u2713";
    btn.title = "Pinned for offline use (click to remove)";
  } else if (cached) {
    btn.textContent = "\u25CB";
    btn.title = "Auto-cached (click to pin, or will be evicted when space is needed)";
  } else {
    btn.textContent = "\u2B07";
    btn.title = "Download for offline use";
  }
  btn.classList.toggle("cached", pinned);
  btn.classList.toggle("auto-cached", cached && !pinned);
}

export function isCached(filepath) {
  return _cachedPaths.has(filepath);
}

export async function toggleCache(filepath, btn) {
  const pinned = _pinnedPaths.has(filepath);
  const cached = _cachedPaths.has(filepath);
  btn.disabled = true;
  btn.textContent = "\u2026";

  try {
    if (pinned) {
      // Pinned → remove entirely
      await evictPdf(filepath);
      _cachedPaths.delete(filepath);
      _pinnedPaths.delete(filepath);
    } else {
      // Not pinned (uncached or auto-cached) → pin it
      const result = await cachePdf(filepath);
      if (result.ok) {
        _cachedPaths.add(filepath);
        _pinnedPaths.add(filepath);
      } else {
        throw new Error(result.error || "Cache failed");
      }
    }
  } catch (err) {
    console.error("Cache toggle failed:", err);
  } finally {
    btn.disabled = false;
    applyCacheButtonState(btn, filepath);
  }
}

// ---------------------------------------------------------------------------
// Offline dialog handlers
// ---------------------------------------------------------------------------

export function initCacheUI() {
  const btnOffline = document.getElementById("btn-offline");
  const offlineDialog = document.getElementById("offline-dialog");
  const offlineClose = document.getElementById("offline-close");
  const btnRefreshLibrary = document.getElementById("btn-refresh-library");
  const btnClearPdfs = document.getElementById("btn-clear-pdfs");
  const offlineStatus = document.getElementById("offline-status");

  if (!btnOffline || !offlineDialog) return;

  btnOffline.addEventListener("click", async () => {
    offlineStatus.textContent = "Checking cache\u2026";
    offlineDialog.showModal();
    const status = await getCacheStatus();
    const pinCount = status.pinned.size;
    const autoCount = status.cached.size - pinCount;
    const parts = [];
    if (pinCount > 0) parts.push(`${pinCount} pinned`);
    if (autoCount > 0) parts.push(`${autoCount} auto-cached`);
    offlineStatus.textContent = parts.length > 0
      ? `${status.cached.size} PDFs cached (${parts.join(", ")}). Auto-cache limit: ${MAX_AUTO_CACHED}.`
      : "No PDFs cached.";
  });

  offlineClose.addEventListener("click", () => offlineDialog.close());

  btnRefreshLibrary.addEventListener("click", async () => {
    btnRefreshLibrary.disabled = true;
    btnRefreshLibrary.textContent = "Refreshing\u2026";
    try {
      const result = await cacheLibrary();
      offlineStatus.textContent = result.ok
        ? "Library cached for offline use."
        : `Failed: ${result.error}`;
    } catch (err) {
      offlineStatus.textContent = `Failed: ${err.message}`;
    } finally {
      btnRefreshLibrary.disabled = false;
      btnRefreshLibrary.textContent = "Refresh Library Cache";
    }
  });

  btnClearPdfs.addEventListener("click", async () => {
    btnClearPdfs.disabled = true;
    btnClearPdfs.textContent = "Clearing\u2026";
    try {
      await clearPdfCache();
      _cachedPaths.clear();
      updateCacheButtons();
      offlineStatus.textContent = "PDF cache cleared.";
    } catch (err) {
      offlineStatus.textContent = `Failed: ${err.message}`;
    } finally {
      btnClearPdfs.disabled = false;
      btnClearPdfs.textContent = "Clear PDF Cache";
    }
  });
}
