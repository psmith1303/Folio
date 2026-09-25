// Single source of truth for the shell build. Keep this in lockstep with
// the FastAPI `version=` in web/server.py — the client compares the two to
// detect (and self-heal) a stale service-worker shell.
const APP_VERSION = "2.15.0";
const SHELL_CACHE = "folio-v" + APP_VERSION;
// Deliberately NOT keyed by APP_VERSION. Cached API responses are user data
// (the library snapshot that makes an offline launch possible), not part of
// the shell. Keying them by version meant every release silently discarded
// the offline library and it only came back after being online again.
// v2: responses hold library-relative paths since 2.14.0; v1's absolute ones
// are swept on activate along with the v1 PDF cache.
const API_CACHE = "folio-api-v2";
// The LRU database from before paths became relative (see offline-lru.js).
const OLD_LRU_DB = "folio-lru";

// The PDF cache name, its keys and the LRU store, shared with the page
// (cache.js). Versioned URL: an unversioned one could come back stale from
// the HTTP cache when a new service worker installs.
importScripts("/modules/offline-lru.js?v=" + APP_VERSION);
const { PDF_CACHE, pdfCacheKey, touchLruEntry, storePdf } = self.FolioLru;

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
  "/modules/library-filter.js",
  "/modules/offline-lru.js",
  "/modules/score-table.js",
  "/modules/viewer.js",
  "/modules/annotations.js",
  "/modules/annot-outbox.js",
  "/modules/setlists.js",
  "/modules/dialog-handlers.js",
  "/modules/keyboard.js",
  "/modules/touch.js",
  "/modules/cache.js",
  "/modules/recent.js",
  "/modules/newest.js",
  "/modules/stamps.js",
  "/stamps/stamps.json",
  "/stamps/start-page.png",
  "/lib/pdfjs/build/pdf.min.mjs",
  "/lib/pdfjs/build/pdf.worker.min.mjs",
  "/manifest.json",
  "/favicon.ico",
  "/apple-touch-icon.png",
  "/icon-192.png",
  "/icon-512.png",
];

// ---------------------------------------------------------------------------
// URL helpers
// ---------------------------------------------------------------------------

function getPathFromPdfUrl(url) {
  return new URL(url).searchParams.get("path");
}

// ---------------------------------------------------------------------------
// Install / Activate
// ---------------------------------------------------------------------------

self.addEventListener("install", (e) => {
  // Bypass the browser's HTTP cache (not just Cache Storage) — otherwise a
  // heuristically-fresh disk-cached file can slip a stale module into an
  // otherwise-new shell, producing a version mismatch that breaks imports.
  e.waitUntil(
    caches.open(SHELL_CACHE).then((cache) =>
      cache.addAll(SHELL_URLS.map((url) => new Request(url, { cache: "reload" })))
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names
          .filter((n) => n !== SHELL_CACHE && n !== PDF_CACHE && n !== API_CACHE)
          .map((n) => caches.delete(n))
      )
    )
  );
  // Not awaited: an old page still holding a connection blocks the delete
  // until it closes, and activation mustn't wait on that.
  indexedDB.deleteDatabase(OLD_LRU_DB);
  self.clients.claim();
});

// Lets the page query the running shell's build version so it can detect a
// stale client and force an update.
self.addEventListener("message", (e) => {
  if (e.data && e.data.type === "GET_VERSION" && e.ports[0]) {
    e.ports[0].postMessage({ version: APP_VERSION });
  }
});

// ---------------------------------------------------------------------------
// Fetch handlers
// ---------------------------------------------------------------------------

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // PDF fetch: stale-while-revalidate with LRU tracking
  if (url.pathname === "/api/pdf" && e.request.method === "GET") {
    e.respondWith(handlePdfFetch(e.request));
    return;
  }

  // Cached API GETs: network-first with cache fallback.
  //
  // /api/config belongs here even though nothing renders from it directly:
  // initApp() awaits it before it will call loadLibrary() (app.js), so if it
  // cannot be served offline the boot aborts in its catch and the cached
  // library is never even requested. Without this, offline mode only worked
  // if the app was already open when the network went away.
  //
  // /api/library is cached only with NO query string. It is a plain list
  // that every view requests bare (library.js filters client-side, and the
  // song picker searches the same in-memory set). A query string only comes
  // from an old client; API_CACHE, unlike the old version-keyed SHELL_CACHE,
  // is never swept, so caching per-query URLs would leave permanent entries.
  if (
    e.request.method === "GET" &&
    ((url.pathname === "/api/library" && !url.search) ||
      url.pathname === "/api/annotations" ||
      url.pathname === "/api/config")
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

  // App shell: network-first with cache fallback. Force past the browser's
  // HTTP cache — "network-first" is meaningless if a heuristically-fresh
  // disk-cached response can still satisfy it without hitting the network.
  e.respondWith(
    fetch(e.request, { cache: "reload" })
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
  const pdfPath = getPathFromPdfUrl(request.url);
  const cacheKey = pdfCacheKey(pdfPath);
  const cache = await caches.open(PDF_CACHE);

  // Stale-while-revalidate: serve cached immediately, refresh in background
  const cached = await cache.match(cacheKey);
  if (cached) {
    if (pdfPath) {
      touchLruEntry(pdfPath, 0, false).catch(() => {});
      fetchAndStoreInBackground(pdfPath, cached.headers.get("etag"));
    }
    return cached;
  }

  // Cache miss — fetch from network. Retries happen at the viewer layer,
  // which can also purge the cache between attempts to self-heal corruption.
  // Stored before answering, so the page's "Download for offline" (cachePdf)
  // finds it cached when its fetch returns, and needn't store it again.
  try {
    const resp = await fetch(request);

    if (pdfPath) {
      if (resp.status === 200) {
        // Caching is a side effect of a good download, not a condition of
        // returning it (as in handleApiGetFetch): a failed write, most
        // likely QuotaExceededError, used to reach the catch below and
        // answer the viewer 503 "Offline" with the PDF in hand.
        await storePdf(pdfPath, resp, false).catch((err) => {
          console.warn(`[sw] not caching ${pdfPath}:`, err);
        });
      } else if (resp.status === 206) {
        fetchAndStoreInBackground(pdfPath);  // the whole PDF, for next time
      }
    }

    return resp;
  } catch {
    return new Response("Offline — PDF not cached", {
      status: 503,
      headers: { "Content-Type": "text/plain" },
    });
  }
}

// Fetch the whole PDF and store it (auto-cached). Given the cached copy's
// *etag*, it revalidates: the server answers 304 when that copy is current,
// so viewing a cached PDF doesn't download it again, and only a changed PDF
// (a 200) is stored. Its use was already recorded when it was served.
function fetchAndStoreInBackground(pdfPath, etag) {
  fetch(pdfCacheKey(pdfPath), {
    cache: "no-store",
    headers: etag ? { "If-None-Match": etag } : {},
  }).then((resp) => {
    if (resp.status === 200) return storePdf(pdfPath, resp, false);
  }).catch(() => {});
}

async function handleApiGetFetch(request) {
  let resp;
  try {
    resp = await fetch(request, { cache: "no-store" });
  } catch {
    return handleApiGetFetchOffline(request);
  }
  if (resp.ok) {
    // Caching is a side effect of a successful fetch, not a condition of
    // returning one. A cache write can fail on its own (QuotaExceededError,
    // most likely) — if that failure shared the fetch's try/catch, it used
    // to discard a perfectly good response and answer with stale data or a
    // 503 while the server had just answered correctly.
    try {
      const cache = await caches.open(API_CACHE);
      await cache.put(request, resp.clone());
    } catch (err) {
      console.warn(`[sw] not caching ${request.url}:`, err);
    }
  }
  return resp;
}

async function handleApiGetFetchOffline(request) {
  const cache = await caches.open(API_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;
  return new Response(JSON.stringify({ error: "Offline" }), {
    status: 503,
    headers: { "Content-Type": "application/json" },
  });
}

