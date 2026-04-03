const SHELL_CACHE = "folio-v9";
const PDF_CACHE = "folio-pdfs-v1";
const MAX_AUTO_CACHED = 30;

const SHELL_URLS = [
  "/",
  "/app.css",
  "/app.js",
  "/modules/state.js",
  "/modules/utils.js",
  "/modules/dom.js",
  "/modules/api.js",
  "/modules/views.js",
  "/modules/theme.js",
  "/modules/library.js",
  "/modules/viewer.js",
  "/modules/annotations.js",
  "/modules/setlists.js",
  "/modules/dialog-handlers.js",
  "/modules/keyboard.js",
  "/modules/touch.js",
  "/modules/cache.js",
  "/modules/recent.js",
  "/manifest.json",
  "/favicon.ico",
  "/apple-touch-icon.png",
  "/icon-192.png",
  "/icon-512.png",
];

// ---------------------------------------------------------------------------
// IndexedDB helpers for LRU metadata
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
        // Preserve pinned status: only upgrade to pinned, never downgrade
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

// ---------------------------------------------------------------------------
// LRU eviction — only evict unpinned (auto-cached) entries
// ---------------------------------------------------------------------------

async function evictIfNeeded() {
  const entries = await getAllLruEntries();
  const unpinned = entries
    .filter((e) => !e.pinned)
    .sort((a, b) => a.lastUsed - b.lastUsed);

  if (unpinned.length <= MAX_AUTO_CACHED) return;

  const cache = await caches.open(PDF_CACHE);
  const toEvict = unpinned.slice(0, unpinned.length - MAX_AUTO_CACHED);
  for (const entry of toEvict) {
    const cacheKey = "/api/pdf?path=" + encodeURIComponent(entry.path);
    await cache.delete(cacheKey);
    await removeLruEntry(entry.path);
  }
}

// ---------------------------------------------------------------------------
// URL helpers
// ---------------------------------------------------------------------------

function pdfCacheKey(url) {
  const path = new URL(url).searchParams.get("path");
  return "/api/pdf?path=" + encodeURIComponent(path);
}

function getPathFromPdfUrl(url) {
  return new URL(url).searchParams.get("path");
}

// ---------------------------------------------------------------------------
// Install / Activate
// ---------------------------------------------------------------------------

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_URLS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names
          .filter((n) => n !== SHELL_CACHE && n !== PDF_CACHE)
          .map((n) => caches.delete(n))
      )
    )
  );
  self.clients.claim();
});

// ---------------------------------------------------------------------------
// Fetch handlers
// ---------------------------------------------------------------------------

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // PDF fetch: cache-first with LRU tracking
  if (url.pathname === "/api/pdf" && e.request.method === "GET") {
    e.respondWith(handlePdfFetch(e.request));
    return;
  }

  // Library/annotations GET: network-first with cache fallback
  if (
    e.request.method === "GET" &&
    (url.pathname === "/api/library" || url.pathname === "/api/annotations")
  ) {
    e.respondWith(handleApiGetFetch(e.request));
    return;
  }

  // Other API calls: pass through
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  // CDN resources (pdf.js): cache-first
  if (url.origin !== self.location.origin) {
    e.respondWith(
      caches.match(e.request).then((cached) => {
        if (cached) return cached;
        return fetch(e.request).then((resp) => {
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(SHELL_CACHE).then((c) => c.put(e.request, clone));
          }
          return resp;
        });
      })
    );
    return;
  }

  // App shell: network-first with cache fallback
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        if (resp.ok) {
          const clone = resp.clone();
          caches.open(SHELL_CACHE).then((c) => c.put(e.request, clone));
        }
        return resp;
      })
      .catch(() => caches.match(e.request))
  );
});

async function handlePdfFetch(request) {
  const cacheKey = pdfCacheKey(request.url);
  const pdfPath = getPathFromPdfUrl(request.url);
  const cache = await caches.open(PDF_CACHE);

  // Check cache for full response
  const cached = await cache.match(cacheKey);
  if (cached) {
    // Return full cached response (works even if browser sent Range header)
    if (pdfPath) touchLruEntry(pdfPath, 0, false).catch(() => {});
    return cached;
  }

  // Not cached — pass original request through to network
  const resp = await fetch(request);

  if (pdfPath) {
    if (resp.status === 200) {
      // Full response — cache directly
      const clone = resp.clone();
      await cache.put(cacheKey, clone);
      const size = parseInt(resp.headers.get("content-length") || "0", 10);
      await touchLruEntry(pdfPath, size, false);
      evictIfNeeded().catch(() => {});
    } else if (resp.status === 206) {
      // Partial (Range) response — fetch full file in background for caching
      cacheFullPdfInBackground(pdfPath, cacheKey);
    }
  }

  return resp;
}

function cacheFullPdfInBackground(pdfPath, cacheKey) {
  const url = "/api/pdf?path=" + encodeURIComponent(pdfPath);
  fetch(url).then(async (resp) => {
    if (resp.status !== 200) return;
    const cache = await caches.open(PDF_CACHE);
    await cache.put(cacheKey, resp);
    const size = parseInt(resp.headers.get("content-length") || "0", 10);
    await touchLruEntry(pdfPath, size, false);
    await evictIfNeeded();
  }).catch(() => {});
}

async function handleApiGetFetch(request) {
  try {
    const resp = await fetch(request);
    if (resp.ok) {
      const clone = resp.clone();
      const cache = await caches.open(SHELL_CACHE);
      await cache.put(request, clone);
    }
    return resp;
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    return new Response(JSON.stringify({ error: "Offline" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}

