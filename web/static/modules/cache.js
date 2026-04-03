// ---------------------------------------------------------------------------
// Offline cache management — direct Cache API + IndexedDB from page context
// ---------------------------------------------------------------------------

import { libraryBody } from "./dom.js";

const PDF_CACHE = "folio-pdfs-v1";
const MAX_AUTO_CACHED = 30;

// Cache API and Service Workers require a secure context (HTTPS or localhost).
export const CACHE_AVAILABLE = window.isSecureContext && "caches" in window;

// ---------------------------------------------------------------------------
// IndexedDB helpers (same schema as sw.js, shared database)
// ---------------------------------------------------------------------------

function openLruDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("folio-lru", 2);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("entries")) {
        db.createObjectStore("entries", { keyPath: "path" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function touchLruEntry(path, size, pinned) {
  const db = await openLruDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("entries", "readwrite");
    const store = tx.objectStore("entries");
    const getReq = store.get(path);
    getReq.onsuccess = () => {
      const existing = getReq.result;
      store.put({
        path,
        lastUsed: Date.now(),
        size: size || (existing && existing.size) || 0,
        pinned: pinned || (existing && existing.pinned) || false,
      });
    };
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function removeLruEntry(path) {
  const db = await openLruDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("entries", "readwrite");
    tx.objectStore("entries").delete(path);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function getAllLruEntries() {
  const db = await openLruDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("entries", "readonly");
    const req = tx.objectStore("entries").getAll();
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function clearAllLruEntries() {
  const db = await openLruDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("entries", "readwrite");
    tx.objectStore("entries").clear();
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

async function evictIfNeeded() {
  const entries = await getAllLruEntries();
  const unpinned = entries
    .filter((e) => !e.pinned)
    .sort((a, b) => a.lastUsed - b.lastUsed);
  if (unpinned.length <= MAX_AUTO_CACHED) return;

  const cache = await caches.open(PDF_CACHE);
  const toEvict = unpinned.slice(0, unpinned.length - MAX_AUTO_CACHED);
  for (const entry of toEvict) {
    await cache.delete("/api/pdf?path=" + encodeURIComponent(entry.path));
    await removeLruEntry(entry.path);
  }
}

// ---------------------------------------------------------------------------
// Public API — all operations done directly, no SW messaging
// ---------------------------------------------------------------------------

export async function cachePdf(path) {
  const cacheKey = "/api/pdf?path=" + encodeURIComponent(path);
  const url = cacheKey + "&_t=" + Date.now();
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const cache = await caches.open(PDF_CACHE);
  await cache.put(cacheKey, resp);
  const size = parseInt(resp.headers.get("content-length") || "0", 10);
  await touchLruEntry(path, size, true);
  await evictIfNeeded();
}

export async function evictPdf(path) {
  const cache = await caches.open(PDF_CACHE);
  await cache.delete("/api/pdf?path=" + encodeURIComponent(path));
  await removeLruEntry(path);
}

export async function getCacheStatus() {
  try {
    const entries = await getAllLruEntries();
    return {
      cached: new Set(entries.map((e) => e.path)),
      pinned: new Set(entries.filter((e) => e.pinned).map((e) => e.path)),
    };
  } catch {
    return { cached: new Set(), pinned: new Set() };
  }
}

export async function clearPdfCache() {
  await caches.delete(PDF_CACHE);
  await clearAllLruEntries();
}

export async function cacheLibrary() {
  // Fetch triggers the SW's handleApiGetFetch which caches the response
  const resp = await fetch("/api/library");
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
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
    btn.title = "Auto-cached (click to pin)";
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
  btn.disabled = true;
  btn.textContent = "\u2026";

  try {
    if (pinned) {
      await evictPdf(filepath);
      _cachedPaths.delete(filepath);
      _pinnedPaths.delete(filepath);
    } else {
      await cachePdf(filepath);
      _cachedPaths.add(filepath);
      _pinnedPaths.add(filepath);
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

  if (!CACHE_AVAILABLE) {
    btnOffline.title = "Offline caching requires HTTPS";
    // Hide cache column header in library table
    const cacheHeader = document.querySelector("th.cache-col");
    if (cacheHeader) cacheHeader.classList.add("hidden");
  }

  btnOffline.addEventListener("click", async () => {
    offlineDialog.showModal();
    if (!CACHE_AVAILABLE) {
      offlineStatus.textContent =
        "Offline caching requires HTTPS. Access Folio via https:// or localhost.";
      btnRefreshLibrary.disabled = true;
      btnClearPdfs.disabled = true;
      return;
    }
    offlineStatus.textContent = "Checking cache\u2026";
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
      await cacheLibrary();
      offlineStatus.textContent = "Library cached for offline use.";
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
      _pinnedPaths.clear();
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
