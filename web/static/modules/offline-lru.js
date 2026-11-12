// ---------------------------------------------------------------------------
// Offline PDF cache bookkeeping, shared by the service worker and the page.
//
// Cached PDFs live in Cache Storage under PDF_CACHE, keyed by pdfCacheKey;
// the LRU_DB IndexedDB store records, per path, when it was last used, its
// size, whether the user pinned it (pinned) and whether a setlist holds it
// (held: setlist PDFs are pinned automatically, and released when no
// setlist holds them any more). Only PDFs that are neither (auto-cached) are
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
  const MAX_AUTO_CACHED = 500;
  // What this copy of the LRU store understands. The service worker reports
  // it, and the page syncs setlist holds only when the two agree: an older
  // worker drops `held` on every touch and evicts held PDFs. Bump it when an
  // older worker would mishandle entries this one writes.
  const LRU_SCHEMA = 2;  // 2: held

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

  // Run *work(store, out)* in one transaction on the entries store; resolves
  // to whatever it left in `out.result` once the transaction has committed.
  async function inStore(mode, work) {
    const db = await openLruDb();
    return new Promise((resolve, reject) => {
      const tx = db.transaction("entries", mode);
      const out = { result: undefined };
      work(tx.objectStore("entries"), out);
      tx.oncomplete = () => resolve(out.result);
      tx.onerror = () => reject(tx.error);
    });
  }

  // Read-modify-write *path*'s entry in one transaction, so a concurrent
  // touch can't be lost. *change(existing)* returns the new entry, or null
  // to leave it as it is. Fields it doesn't name are kept.
  function modifyLruEntry(path, change) {
    return inStore("readwrite", (store) => {
      const req = store.get(path);
      req.onsuccess = () => {
        const next = change(req.result);
        if (next) store.put({ ...req.result, ...next, path });
      };
    });
  }

  function touchLruEntry(path, size, pinned) {
    return modifyLruEntry(path, (existing) => ({
      lastUsed: Date.now(),
      // size 0 keeps the recorded size (a pin of an already-cached PDF)
      size: size || (existing && existing.size) || 0,
      // Preserve pinned status: only upgrade to pinned, never downgrade
      pinned: pinned || (existing && existing.pinned) || false,
    }));
  }

  // Mark *paths* (oldest first) as just used, in that order, in one
  // transaction. Entries not in the store are skipped.
  function touchInOrder(paths) {
    const now = Date.now();
    return inStore("readwrite", (store) => {
      paths.forEach((path, i) => {
        const req = store.get(path);
        req.onsuccess = () => {
          if (req.result) store.put({ ...req.result, lastUsed: now + i });
        };
      });
    });
  }

  // Drop the user's pin on *path*, keeping the cached copy (auto-cached, or
  // still pinned by a setlist).
  function unpinManual(path) {
    return modifyLruEntry(path, (existing) => (existing ? { pinned: false } : null));
  }

  // Mark exactly the entries in *paths* (a Set) as held by a setlist, and
  // release the rest. Only entries whose flag changes are written; resolves
  // to how many were.
  function setHeld(paths) {
    return inStore("readwrite", (store, out) => {
      out.result = 0;
      const req = store.getAll();
      req.onsuccess = () => {
        for (const e of req.result) {
          const held = paths.has(e.path);
          if (held !== Boolean(e.held)) {
            store.put({ ...e, held });
            out.result++;
          }
        }
      };
    });
  }

  function removeLruEntry(path) {
    return inStore("readwrite", (store) => { store.delete(path); });
  }

  function getAllLruEntries() {
    return inStore("readonly", (store, out) => {
      const req = store.getAll();
      req.onsuccess = () => { out.result = req.result; };
    });
  }

  function clearAllLruEntries() {
    return inStore("readwrite", (store) => { store.clear(); });
  }

  async function isPdfCached(path) {
    const cache = await caches.open(PDF_CACHE);
    return Boolean(await cache.match(pdfCacheKey(path)));
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
    if (!(await isPdfCached(path))) return false;
    await touchLruEntry(path, 0, true);  // size 0 keeps the recorded size
    return true;
  }

  // Evict the least recently used auto-cached PDFs (neither pinned nor
  // held) beyond MAX_AUTO_CACHED.
  async function evictIfNeeded() {
    const entries = await getAllLruEntries();
    const unpinned = entries
      .filter((e) => !e.pinned && !e.held)
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
    PDF_CACHE, MAX_AUTO_CACHED, LRU_SCHEMA, pdfCacheKey,
    touchLruEntry, touchInOrder, removeLruEntry, getAllLruEntries, clearAllLruEntries,
    evictIfNeeded, storePdf, isPdfCached, pinIfCached, unpinManual, setHeld,
  };
})();
