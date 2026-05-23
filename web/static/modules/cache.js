// ---------------------------------------------------------------------------
// Offline cache management — direct Cache API + IndexedDB from page context
// ---------------------------------------------------------------------------

import { libraryBody } from "./dom.js";

const PDF_CACHE = "folio-pdfs-v1";
const MAX_AUTO_CACHED = 100;

// Cache API and Service Workers require a secure context (HTTPS or localhost).
export const CACHE_AVAILABLE = window.isSecureContext && "caches" in window;

// ---------------------------------------------------------------------------
// Cache-state icons (inline SVG)
//
// Inline SVG renders identically on every platform. The previous Unicode
// glyphs (download arrow, check, circle) were drawn as colour emoji on iOS
// but monochrome text on desktop, so the cache-state icons looked different
// on iPad vs PC. SVG uses currentColor, so the existing .cached /
// .auto-cached colour rules still apply.
// ---------------------------------------------------------------------------

const _icon = (inner) =>
  '<svg class="cache-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor"' +
  ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  inner + "</svg>";

// Not cached — download (down arrow over a tray line).
export const ICON_NOT_CACHED = _icon('<path d="M8 2.5v7"/><path d="M5 7l3 3 3-3"/><path d="M3.5 13h9"/>');
// Auto-cached (present but not pinned) — hollow circle.
export const ICON_AUTO_CACHED = _icon('<circle cx="8" cy="8" r="5"/>');
// Pinned for offline — check mark.
export const ICON_PINNED = _icon('<path d="M3.5 8.5l3.5 3.5 5.5-7"/>');

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

  // Read the whole body before caching so we can verify it's complete.
  // A streaming cache.put on a mid-flight-truncated response (e.g. Tailscale
  // proxy dropping the connection) silently stores partial bytes, producing
  // a "cached" PDF that later fails with "Bad end offset" in pdf.js.
  const expected = parseInt(resp.headers.get("content-length") || "0", 10);
  const buf = await resp.arrayBuffer();
  if (expected > 0 && buf.byteLength !== expected) {
    throw new Error(
      `Incomplete download: got ${buf.byteLength} of ${expected} bytes — not caching`
    );
  }

  const verified = new Response(buf, {
    status: resp.status,
    statusText: resp.statusText,
    headers: resp.headers,
  });
  const cache = await caches.open(PDF_CACHE);
  await cache.put(cacheKey, verified);
  await touchLruEntry(path, buf.byteLength, true);
  await evictIfNeeded();

  // Keep the in-memory status sets in sync so any view rendered afterwards
  // (e.g. switching to Newest after a "Cache setlist") reflects the new state
  // without waiting on a refresh. Callers that batch-cache (setlist download)
  // bypass toggleCache, which is the only other place these sets are updated.
  _cachedPaths.add(path);
  _pinnedPaths.add(path);
}

export async function evictPdf(path) {
  const cache = await caches.open(PDF_CACHE);
  await cache.delete("/api/pdf?path=" + encodeURIComponent(path));
  await removeLruEntry(path);
  _cachedPaths.delete(path);
  _pinnedPaths.delete(path);
}

// Pin a PDF for offline use. If it's already in the cache (e.g. auto-cached
// after viewing — pinned:false), just flip the pinned flag instead of
// re-downloading. This is what "Cache setlist" and the per-row toggle use so
// that already-cached-but-unpinned PDFs actually get pinned.
export async function pinPdf(path) {
  const cacheKey = "/api/pdf?path=" + encodeURIComponent(path);
  const cache = await caches.open(PDF_CACHE);
  const existing = await cache.match(cacheKey);
  if (existing) {
    await touchLruEntry(path, 0, true);  // size 0 → touchLruEntry keeps existing
    _cachedPaths.add(path);
    _pinnedPaths.add(path);
    return;
  }
  await cachePdf(path);  // not cached yet → download and pin
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

export async function refreshCacheStatus(tbody = libraryBody) {
  const status = await getCacheStatus();
  _cachedPaths = status.cached;
  _pinnedPaths = status.pinned;
  updateCacheButtons(tbody);
}

function updateCacheButtons(tbody = libraryBody) {
  const rows = tbody.querySelectorAll("tr");
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
    btn.innerHTML = ICON_PINNED;
    btn.title = "Pinned for offline use (click to remove)";
  } else if (cached) {
    btn.innerHTML = ICON_AUTO_CACHED;
    btn.title = "Auto-cached (click to pin)";
  } else {
    btn.innerHTML = ICON_NOT_CACHED;
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
    } else {
      await pinPdf(filepath);
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
