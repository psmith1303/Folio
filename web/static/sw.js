// Single source of truth for the shell build. Keep this in lockstep with
// the FastAPI `version=` in web/server.py — the client compares the two to
// detect (and self-heal) a stale service-worker shell.
const APP_VERSION = "2.13.10";
const SHELL_CACHE = "folio-v" + APP_VERSION;
// Deliberately NOT keyed by APP_VERSION. Cached API responses are user data
// (the library snapshot that makes an offline launch possible), not part of
// the shell. Keying them by version meant every release silently discarded
// the offline library and it only came back after being online again.
const API_CACHE = "folio-api-v1";

// The PDF cache name, its keys and the LRU store, shared with the page
// (cache.js). Versioned URL: an unversioned one could come back stale from
// the HTTP cache when a new service worker installs.
importScripts("/modules/offline-lru.js?v=" + APP_VERSION);
const { PDF_CACHE, pdfCacheKey, touchLruEntry, evictIfNeeded } = self.FolioLru;

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

// Read the response body fully and verify Content-Length before caching.
// Tailscale (and other proxies) can truncate a streaming response mid-flight;
// cache.put on such a response silently stores partial bytes, producing a
// "cached" PDF that later fails with "Bad end offset" in pdf.js. Validating
// the byte count prevents poisoning the cache with corrupt data.
async function safeCachePut(cache, cacheKey, resp) {
  const expected = parseInt(resp.headers.get("content-length") || "0", 10);
  const buf = await resp.clone().arrayBuffer();
  if (expected > 0 && buf.byteLength !== expected) {
    console.warn(
      `[sw] not caching ${cacheKey}: got ${buf.byteLength} of ${expected} bytes`
    );
    return false;
  }
  const verified = new Response(buf, {
    status: resp.status,
    statusText: resp.statusText,
    headers: resp.headers,
  });
  await cache.put(cacheKey, verified);
  return true;
}

async function handlePdfFetch(request) {
  const pdfPath = getPathFromPdfUrl(request.url);
  const cacheKey = pdfCacheKey(pdfPath);
  const cache = await caches.open(PDF_CACHE);

  // Stale-while-revalidate: serve cached immediately, refresh in background
  const cached = await cache.match(cacheKey);
  if (cached) {
    if (pdfPath) touchLruEntry(pdfPath, 0, false).catch(() => {});
    revalidateInBackground(request, pdfPath, cacheKey);
    return cached;
  }

  // Cache miss — fetch from network. Retries happen at the viewer layer,
  // which can also purge the cache between attempts to self-heal corruption.
  try {
    const resp = await fetch(request);

    if (pdfPath) {
      if (resp.status === 200) {
        const stored = await safeCachePut(cache, cacheKey, resp);
        if (stored) {
          const size = parseInt(resp.headers.get("content-length") || "0", 10);
          await touchLruEntry(pdfPath, size, false);
          evictIfNeeded().catch(() => {});
        }
      } else if (resp.status === 206) {
        cacheFullPdfInBackground(pdfPath, cacheKey);
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

function revalidateInBackground(request, pdfPath, cacheKey) {
  fetch(request).then(async (resp) => {
    if (!pdfPath) return;
    if (resp.status === 200) {
      const cache = await caches.open(PDF_CACHE);
      const stored = await safeCachePut(cache, cacheKey, resp);
      if (stored) {
        const size = parseInt(resp.headers.get("content-length") || "0", 10);
        await touchLruEntry(pdfPath, size, false);
        evictIfNeeded().catch(() => {});
      }
    } else if (resp.status === 206) {
      cacheFullPdfInBackground(pdfPath, cacheKey);
    }
  }).catch(() => {});
}

function cacheFullPdfInBackground(pdfPath, cacheKey) {
  fetch(cacheKey).then(async (resp) => {
    if (resp.status !== 200) return;
    const cache = await caches.open(PDF_CACHE);
    const stored = await safeCachePut(cache, cacheKey, resp);
    if (stored) {
      const size = parseInt(resp.headers.get("content-length") || "0", 10);
      await touchLruEntry(pdfPath, size, false);
      await evictIfNeeded();
    }
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

