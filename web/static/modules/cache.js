// ---------------------------------------------------------------------------
// Offline cache management — direct Cache API + IndexedDB from page context
// ---------------------------------------------------------------------------

import { libraryBody, recentBody, newestBody, setlistSongsBody } from "./dom.js";
import { api, askServiceWorker } from "./api.js";
import "./offline-lru.js";  // defines self.FolioLru, shared with sw.js

// Cache names, keys and the LRU store are shared with the service worker.
// The side-effect import above has run by now (imports evaluate first).
const {
  PDF_CACHE, MAX_AUTO_CACHED, LRU_SCHEMA, pdfCacheKey,
  touchInOrder, getAllLruEntries, clearAllLruEntries, evictIfNeeded,
  storePdf, isPdfCached, pinIfCached, unpinManual, setHeld,
} = self.FolioLru;
export { PDF_CACHE, pdfCacheKey };

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
const ICON_NOT_CACHED = _icon('<path d="M8 2.5v7"/><path d="M5 7l3 3 3-3"/><path d="M3.5 13h9"/>');
// Auto-cached (present but not pinned) — hollow circle.
const ICON_AUTO_CACHED = _icon('<circle cx="8" cy="8" r="5"/>');
// Pinned for offline — check mark.
const ICON_PINNED = _icon('<path d="M3.5 8.5l3.5 3.5 5.5-7"/>');

// ---------------------------------------------------------------------------
// Public API — all operations done directly, no SW messaging
// ---------------------------------------------------------------------------

// Download *path* into the offline cache, pinned by the user or not. One
// download: the fetch goes through the service worker, which stores what it
// fetches (unpinned) before answering. With no service worker, or if it
// didn't store it (a truncated download), it is stored here -- or,
// truncated, not at all.
async function downloadPdf(path, pin) {
  const resp = await fetch(pdfCacheKey(path) + "&_t=" + Date.now());
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const stored = pin ? await pinIfCached(path) : await isPdfCached(path);
  if (stored) {
    resp.body?.cancel().catch(() => {});  // the worker kept its own copy
  } else if (!(await storePdf(path, resp, pin))) {
    throw new Error("Incomplete download — not caching");
  }
}

export async function cachePdf(path) {
  await downloadPdf(path, true);

  // Keep the in-memory status in sync so any view rendered afterwards
  // reflects the new state without waiting on a refresh.
  _status.cached.add(path);
  _status.manual.add(path);
}

// Remove the user's pin on *path*. The copy stays: auto-cached, or still
// pinned if a setlist holds it. (Deleting it would be undone by the next
// sync if it's in Recent; "Clear PDF Cache" deletes everything.)
export async function unpinPdf(path) {
  await unpinManual(path);
  _status.manual.delete(path);
}

// Pin a PDF for offline use. If it's already in the cache (e.g. auto-cached
// after viewing — pinned:false), just flip the pinned flag instead of
// re-downloading. This is what the per-row toggle uses so that
// already-cached-but-unpinned PDFs actually get pinned.
export async function pinPdf(path) {
  if (await pinIfCached(path)) {
    _status.cached.add(path);
    _status.manual.add(path);
    return;
  }
  await cachePdf(path);  // not cached yet → download and pin
}

// Sets of paths: cached (every cached PDF), manual (pinned by the user)
// and held (by a setlist). A PDF is pinned, never evicted, if either.
function emptyStatus() {
  return { cached: new Set(), manual: new Set(), held: new Set() };
}

export async function getCacheStatus() {
  const status = emptyStatus();
  try {
    for (const e of await getAllLruEntries()) {
      status.cached.add(e.path);
      if (e.pinned) status.manual.add(e.path);
      if (e.held) status.held.add(e.path);
    }
  } catch {
    // no IndexedDB: report nothing cached
  }
  return status;
}

const isPinned = (status, path) => status.manual.has(path) || status.held.has(path);

export async function clearPdfCache() {
  await caches.delete(PDF_CACHE);
  await clearAllLruEntries();
}

// Fire-and-forget re-fetch of /api/config, to keep the SW's cached copy from
// going stale. Call after anything that can change library_dir/score_count
// (setting the folder, a rescan) — otherwise a launch that only ever ran
// cacheLibrary() before the change would still read the pre-change snapshot
// offline and bounce to the "pick a folder" dialog instead of the library.
export function refreshCachedConfig() {
  if (!CACHE_AVAILABLE) return;
  fetch("/api/config").catch(() => {});
}

export async function cacheLibrary() {
  // Fetch triggers the SW's handleApiGetFetch, which caches each response.
  // /api/config is included even though nothing here reads it: it is fetched
  // once at boot and never again, so a stale cached copy (score_count: 0,
  // from before the library was set up) permanently sends a cold offline
  // launch to the "pick a folder" dialog instead of the cached library —
  // this is the one control meant to prepare for exactly that launch.
  const [libResp, cfgResp] = await Promise.all([
    fetch("/api/library"),
    fetch("/api/config"),
  ]);
  if (!libResp.ok) throw new Error(`HTTP ${libResp.status}`);
  if (!cfgResp.ok) throw new Error(`HTTP ${cfgResp.status}`);
}

// ---------------------------------------------------------------------------
// Keeping setlists and Recent offline
//
// Each device syncs for itself (the cache lives in its browser): at launch
// and after it changes a setlist. Every setlist's PDFs are held (pinned
// until no setlist holds them); every Recent PDF is cached as an ordinary
// auto-cached entry, its lastUsed bumped so it outranks other auto-cached
// PDFs (Recent's 200 fit within MAX_AUTO_CACHED).
// ---------------------------------------------------------------------------

// Whether the service worker (if any) can be trusted with held entries: an
// older one (no lruSchema in its reply) drops the flag on every touch and
// evicts held PDFs as it stores what the sync downloads. The next launch,
// under the new worker, syncs.
async function workerUnderstandsHolds() {
  if (!(navigator.serviceWorker && navigator.serviceWorker.controller)) return true;
  const reply = await askServiceWorker({ type: "GET_VERSION" });
  return Boolean(reply) && reply.lruSchema === LRU_SCHEMA;
}

async function runOfflineSync() {
  if (!(await workerUnderstandsHolds())) {
    console.info("[offline] sync skipped: the service worker is out of date");
    return;
  }
  let wanted;
  try {
    wanted = await api("/api/offline/wanted");
  } catch (err) {
    // Offline, or the server couldn't read the setlists: leave every hold
    // as it is. An empty list here would release them all.
    console.info("[offline] sync skipped:", err.message || err);
    return;
  }
  const held = new Set(wanted.setlist_paths);
  const recent = [...wanted.recent_paths].reverse();  // oldest first
  let changed = await setHeld(held);
  const { cached } = await getCacheStatus();
  let downloaded = 0;
  const fetchMissing = async (path) => {
    if (cached.has(path) || !(await syncDownload(path))) return false;
    cached.add(path);
    downloaded++;
    return true;
  };
  // Each download stores an unheld, auto-cached entry and may evict the
  // oldest of those. So rank the cached Recent items above other auto-cached
  // PDFs first, and hold each setlist PDF as soon as it lands: the
  // downloads then evict neither (Recent's 200 fit in MAX_AUTO_CACHED).
  try {
    await touchInOrder(recent);
    for (const path of held) {
      if (await fetchMissing(path)) changed += await setHeld(held);
    }
    for (const path of recent) await fetchMissing(path);
  } finally {
    // So the newest Recent item is the most recently used, and every Recent
    // item outranks other auto-cached PDFs.
    await touchInOrder(recent);
    await evictIfNeeded();
    if (changed || downloaded) await refreshAllCacheButtons();
  }
}

// One PDF for the sync; resolves to whether it was stored. A PDF that can't
// be had (404, truncated) is skipped; a network failure (we went offline)
// ends the run.
async function syncDownload(path) {
  try {
    await downloadPdf(path, false);
    return true;
  } catch (err) {
    if (err instanceof TypeError) throw err;
    console.warn(`[offline] sync: not caching ${path}:`, err.message || err);
    return false;
  }
}

let _syncRun = null;
let _syncAgain = false;

// Bring this device's offline cache in line with the setlists and Recent.
// One run at a time: a call during a run queues one more run after it.
// Never rejects.
export function syncOfflineSet() {
  if (!CACHE_AVAILABLE) return Promise.resolve();
  if (_syncRun) {
    _syncAgain = true;
    return _syncRun;
  }
  _syncRun = (async () => {
    do {
      _syncAgain = false;
      try {
        await runOfflineSync();
      } catch (err) {
        console.warn("[offline] sync failed:", err);
      }
    } while (_syncAgain);
    _syncRun = null;
  })();
  return _syncRun;
}

// Ask the browser not to clear this site's storage (PDFs, offline
// annotations) under storage pressure. Safari grants it to home-screen web
// apps. Never rejects.
export async function requestPersistentStorage() {
  if (!CACHE_AVAILABLE || !navigator.storage || !navigator.storage.persist) return;
  try {
    if (await navigator.storage.persisted()) return;
    const granted = await navigator.storage.persist();
    console.info(`[offline] persistent storage ${granted ? "granted" : "not granted"}`);
  } catch (err) {
    console.warn("[offline] persistent storage request failed:", err);
  }
}

// The offline dialog's storage line, or "" if the browser can't say.
async function storageLine() {
  try {
    if (!navigator.storage || !navigator.storage.persisted) return "";
    return (await navigator.storage.persisted())
      ? " Storage: persistent."
      : " Storage: may be cleared by the browser.";
  } catch {
    return "";
  }
}

// ---------------------------------------------------------------------------
// UI — per-row cache buttons in library table
// ---------------------------------------------------------------------------

let _status = emptyStatus();

export async function refreshCacheStatus(tbody = libraryBody) {
  _status = await getCacheStatus();
  updateCacheButtons(tbody);
}

async function refreshAllCacheButtons() {
  await refreshCacheStatus();
  for (const tbody of [recentBody, newestBody, setlistSongsBody]) updateCacheButtons(tbody);
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

// A score's cache button: icon and tooltip, and which state class it has
// ("cached" when pinned, "auto-cached" when cached but not pinned).
function cacheButtonState(filepath) {
  const pinned = isPinned(_status, filepath);
  const auto = !pinned && _status.cached.has(filepath);
  let icon = ICON_PINNED, title;
  if (_status.held.has(filepath)) {
    title = _status.manual.has(filepath)
      ? "Pinned by a setlist and by you (click to remove your pin)"
      : "Pinned by a setlist";
  } else if (pinned) {
    title = "Pinned for offline use (click to unpin)";
  } else if (auto) {
    [icon, title] = [ICON_AUTO_CACHED, "Auto-cached (click to pin)"];
  } else {
    [icon, title] = [ICON_NOT_CACHED, "Download for offline use"];
  }
  return { icon, title, pinned, auto };
}

function applyCacheButtonState(btn, filepath) {
  const { icon, title, pinned, auto } = cacheButtonState(filepath);
  btn.innerHTML = icon;
  btn.title = title;
  btn.classList.toggle("cached", pinned);
  btn.classList.toggle("auto-cached", auto);
}

// The cache button's HTML for a list row, in the state refreshCacheStatus
// would give it. Clicks on it are handled by onCacheButtonClick.
export function cacheButtonHtml(filepath) {
  const { icon, title, pinned, auto } = cacheButtonState(filepath);
  const cls = pinned ? " cached" : auto ? " auto-cached" : "";
  return `<button class="cache-btn small-btn${cls}" title="${title}">${icon}</button>`;
}

// A table's click listener for the cache buttons in its rows (each row
// carries data-filepath). Returns true if the click was on one.
export function onCacheButtonClick(e) {
  const btn = e.target.closest(".cache-btn");
  const tr = btn?.closest("tr[data-filepath]");
  if (!tr) return false;
  toggleCache(tr.dataset.filepath, btn);
  return true;
}

export async function toggleCache(filepath, btn) {
  const manual = _status.manual.has(filepath);
  // Held by a setlist and not pinned by the user: nothing to toggle.
  if (_status.held.has(filepath) && !manual) return;
  btn.disabled = true;
  btn.textContent = "\u2026";

  try {
    if (manual) {
      await unpinPdf(filepath);
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
    const heldCount = status.held.size;
    const manualCount = [...status.manual].filter((p) => !status.held.has(p)).length;
    const autoCount = status.cached.size - heldCount - manualCount;
    const parts = [];
    if (heldCount > 0) parts.push(`${heldCount} pinned by setlists`);
    if (manualCount > 0) parts.push(`${manualCount} pinned by you`);
    if (autoCount > 0) parts.push(`${autoCount} auto-cached`);
    offlineStatus.textContent = (parts.length > 0
      ? `${status.cached.size} PDFs cached (${parts.join(", ")}). Auto-cache limit: ${MAX_AUTO_CACHED}.`
      : "No PDFs cached.") + (await storageLine());
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
      _status = emptyStatus();
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
