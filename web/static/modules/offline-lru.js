// ---------------------------------------------------------------------------
// Offline PDF cache bookkeeping, shared by the service worker and the page.
//
// Cached PDFs live in Cache Storage under PDF_CACHE, keyed by pdfCacheKey;
// the LRU_DB IndexedDB store records, per path, when it was last used, its
// size and whether the user pinned it. Only unpinned (auto-cached) PDFs are
// evicted, oldest first, beyond MAX_AUTO_CACHED.
//
// Both names end in -v2 since paths became library-relative (2.14.0). The v1
// cache and "folio-lru" database held absolute paths; the service worker
// drops them on activate, so offline copies start fresh.
//
// This is a classic script, not an ES module, because the service worker is
// a classic worker: sw.js loads it with importScripts() and cache.js with a
// side-effect import. Either way it defines self.FolioLru.
// ---------------------------------------------------------------------------

self.FolioLru = (() => {
  const PDF_CACHE = "folio-pdfs-v2";
  const LRU_DB = "folio-lru-v2";
  const MAX_AUTO_CACHED = 100;

  // Cache Storage key for a PDF. The page and the service worker must build
  // the same key, or neither can find what the other cached.
  function pdfCacheKey(path) {
    return "/api/pdf?path=" + encodeURIComponent(path);
  }

  function openLruDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(LRU_DB, 1);
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
          // size 0 keeps the recorded size (a pin of an already-cached PDF)
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

  // Store a fetched PDF (a 200 response) for offline use, pinned or
  // auto-cached, and evict if that takes the cache over its limit. Returns
  // false, storing nothing, if the body is shorter than its Content-Length:
  // a proxy (Tailscale) can truncate a response mid-flight, and a partial
  // copy fails later in pdf.js with "Bad end offset". Reads a clone, so
  // *resp* can still be returned to the page.
  async function storePdf(path, resp, pinned) {
    const expected = parseInt(resp.headers.get("content-length") || "0", 10);
    const buf = await resp.clone().arrayBuffer();
    if (expected > 0 && buf.byteLength !== expected) {
      console.warn(`[offline] not caching ${path}: got ${buf.byteLength} of ${expected} bytes`);
      return false;
    }
    const cache = await caches.open(PDF_CACHE);
    await cache.put(pdfCacheKey(path), new Response(buf, {
      status: resp.status,
      statusText: resp.statusText,
      headers: resp.headers,
    }));
    await touchLruEntry(path, buf.byteLength, pinned);
    // Housekeeping: a failure here mustn't fail storing this PDF.
    evictIfNeeded().catch((err) => console.warn("[offline] eviction failed:", err));
    return true;
  }

  // Pin *path* if it's already cached (e.g. auto-cached after viewing),
  // without downloading it again. Returns whether it was.
  async function pinIfCached(path) {
    const cache = await caches.open(PDF_CACHE);
    if (!(await cache.match(pdfCacheKey(path)))) return false;
    await touchLruEntry(path, 0, true);  // size 0 keeps the recorded size
    return true;
  }

  // Evict the least recently used unpinned PDFs beyond MAX_AUTO_CACHED.
  async function evictIfNeeded() {
    const entries = await getAllLruEntries();
    const unpinned = entries
      .filter((e) => !e.pinned)
      .sort((a, b) => a.lastUsed - b.lastUsed);
    if (unpinned.length <= MAX_AUTO_CACHED) return;

    const cache = await caches.open(PDF_CACHE);
    const toEvict = unpinned.slice(0, unpinned.length - MAX_AUTO_CACHED);
    for (const entry of toEvict) {
      await cache.delete(pdfCacheKey(entry.path));
      await removeLruEntry(entry.path);
    }
  }

  return {
    PDF_CACHE, MAX_AUTO_CACHED, pdfCacheKey,
    touchLruEntry, removeLruEntry, getAllLruEntries, clearAllLruEntries, evictIfNeeded,
    storePdf, pinIfCached,
  };
})();
