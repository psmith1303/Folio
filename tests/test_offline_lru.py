"""Offline PDF cache bookkeeping, shared by the service worker and the page.

web/static/modules/offline-lru.js (Step 10a) is the one copy of the cache
name, the PDF cache keys and the "folio-lru-v2" IndexedDB store: when each
cached PDF was last used, its size, whether the user pinned it and
whether a setlist holds it. Only PDFs that are neither (auto-cached) are
evicted, oldest first, beyond MAX_AUTO_CACHED. sw.js loads it with
importScripts(); cache.js imports it.

These run it the three ways it is used, under Deno with in-memory
IndexedDB and Cache Storage: its functions directly; the REAL sw.js,
installing and serving PDFs; and the page's cachePdf/pinPdf/unpinPdf.
"""

import json
from pathlib import Path

import pytest

from deno_harness import requires_deno, run_deno
from js_module_harness import MODULES, run_module

pytestmark = requires_deno

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
LRU_JS = MODULES / "offline-lru.js"
SW_JS = STATIC / "sw.js"

# In-memory IndexedDB ("folio-lru-v2" entries) and Cache Storage. Cache keys are
# stored as the page and service worker give them (a URL string or Request).
FAKES = r"""
globalThis.__lru = new Map();
let __now = 1000;
Date.now = () => __now;
globalThis.__setNow = (t) => { __now = t; };
const __done = (req, result) => setTimeout(() => { req.result = result; req.onsuccess?.(); }, 0);
// Like real IndexedDB, transactions on the store run one after another: a
// transaction's requests start only when the previous one has completed.
let __txChain = Promise.resolve();
const __tx = () => {
  const turn = __txChain;
  let release;
  __txChain = new Promise((r) => { release = r; });
  const run = (fn) => { turn.then(fn); };
  const tx = { objectStore: () => ({
    getAll: () => { const r = {}; run(() => __done(r, [...__lru.values()].map((e) => ({ ...e })))); return r; },
    get: (k) => { const r = {}; run(() => __done(r, __lru.has(k) ? { ...__lru.get(k) } : undefined)); return r; },
    put: (v) => { run(() => __lru.set(v.path, { ...v })); },
    delete: (k) => { run(() => __lru.delete(k)); },
    clear: () => { run(() => __lru.clear()); },
  }) };
  turn.then(() => setTimeout(() => { tx.oncomplete?.(); release(); }, 5));
  return tx;
};
globalThis.indexedDB = {
  open: () => { const r = {}; __done(r, { transaction: __tx, objectStoreNames: { contains: () => true } }); return r; },
};
globalThis.__stores = new Map();
const __key = (k) => (typeof k === "string" ? k : new URL(k.url).pathname + new URL(k.url).search);
Object.defineProperty(globalThis, "caches", { configurable: true, value: {
  open: async (name) => {
    if (!__stores.has(name)) __stores.set(name, new Map());
    const m = __stores.get(name);
    return {
      match: async (k) => m.get(__key(k))?.clone(),
      put: async (k, v) => { m.set(__key(k), v); },
      delete: async (k) => m.delete(__key(k)),
      addAll: async (reqs) => { for (const r of reqs) m.set(__key(r), new Response("shell")); },
      keys: async () => [...m.keys()],
    };
  },
  keys: async () => [...__stores.keys()],
  delete: async (n) => __stores.delete(n),
  match: async () => undefined,
} });
globalThis.__fetched = [];
globalThis.fetch = async (req) => {
  const url = typeof req === "string" ? req : req.url;
  __fetched.push(url);
  const body = "%PDF-1.4 " + url;
  return new Response(body, { status: 200, headers: { "content-length": String(body.length) } });
};
const __tick = (ms = 30) => new Promise((r) => setTimeout(r, ms));
// A cached PDF and its LRU entry, seeded straight into the fakes (one
// touchLruEntry per PDF would take seconds for MAX_AUTO_CACHED of them).
// Reads the cache name and key from self.FolioLru, so call it after loading.
globalThis.__seedCached = async (path, lastUsed, fields = {}) => {
  __lru.set(path, { path, lastUsed, size: 1, pinned: false, ...fields });
  await (await caches.open(self.FolioLru.PDF_CACHE)).put(self.FolioLru.pdfCacheKey(path), new Response("x"));
};
globalThis.__state = () => ({
  // held is shown only when set, so entries no setlist touched read as before.
  lru: Object.fromEntries([...__lru].map(([p, e]) => [p, { size: e.size, pinned: e.pinned, lastUsed: e.lastUsed,
    ...(e.held ? { held: true } : {}) }])),
  stores: Object.fromEntries([...__stores].map(([n, m]) => [n, [...m.keys()].sort()])),
});
"""


def _lru(body: str):
    """Run *body* with the shared script loaded (as the page loads it)."""
    return run_deno(FAKES + f'await import("{LRU_JS.as_uri()}");\nconst L = self.FolioLru;\n' + body)


def _key(path: str) -> str:
    from urllib.parse import quote
    return "/api/pdf?path=" + quote(path, safe="-_.!~*'()")


# ---------------------------------------------------------------------------
# The shared functions
# ---------------------------------------------------------------------------


def test_touch_records_size_and_last_use():
    r = _lru("""
await L.touchLruEntry("/a.pdf", 500, false);
__setNow(2000); await L.touchLruEntry("/a.pdf", 0, false);    // size 0 keeps it
console.log(JSON.stringify(__state().lru));
""")
    assert r == {"/a.pdf": {"size": 500, "pinned": False, "lastUsed": 2000}}


def test_a_pin_is_never_undone_by_a_later_auto_touch():
    """Viewing a pinned score touches it unpinned; it must stay pinned."""
    r = _lru("""
await L.touchLruEntry("/a.pdf", 500, true);
await L.touchLruEntry("/a.pdf", 700, false);
console.log(JSON.stringify(__state().lru));
""")
    assert r == {"/a.pdf": {"size": 700, "pinned": True, "lastUsed": 1000}}


def test_remove_and_clear():
    r = _lru("""
for (const p of ["/a.pdf", "/b.pdf", "/c.pdf"]) await L.touchLruEntry(p, 1, false);
await L.removeLruEntry("/b.pdf");
const after = Object.keys(__state().lru);
await L.clearAllLruEntries();
console.log(JSON.stringify([after, (await L.getAllLruEntries()).length]));
""")
    assert r == [["/a.pdf", "/c.pdf"], 0]


MAX = 500  # MAX_AUTO_CACHED; test_eviction_... checks it


def _fill(unpinned: int, pinned: list[int], held: list[int] = ()) -> str:
    """Cache `unpinned` auto-cached PDFs /u<i>.pdf used at time i, plus pinned
    PDFs /p<i>.pdf and setlist-held PDFs /h<i>.pdf used at the given (old)
    times."""
    return f"""
for (let i = 0; i < {unpinned}; i++) await __seedCached(`/u${{i}}.pdf`, 10 + i);
for (const t of {json.dumps(list(pinned))}) await __seedCached(`/p${{t}}.pdf`, t, {{ pinned: true }});
for (const t of {json.dumps(list(held))}) await __seedCached(`/h${{t}}.pdf`, t, {{ held: true }});
"""


def test_eviction_removes_the_oldest_unpinned_beyond_the_limit():
    r = _lru(_fill(MAX + 3, [1, 2]) + """
await L.evictIfNeeded();
const s = __state();
console.log(JSON.stringify({ max: L.MAX_AUTO_CACHED, lru: Object.keys(s.lru), cached: s.stores[L.PDF_CACHE] }));
""")
    assert r["max"] == MAX
    kept = [f"/u{i}.pdf" for i in range(3, MAX + 3)] + ["/p1.pdf", "/p2.pdf"]
    assert sorted(r["lru"]) == sorted(kept)
    assert r["cached"] == sorted(_key(p) for p in kept)     # evicted PDFs leave the cache too


def test_at_the_limit_nothing_is_evicted():
    r = _lru(_fill(MAX, []) + """
await L.evictIfNeeded();
console.log(JSON.stringify(Object.keys(__state().lru).length));
""")
    assert r == MAX


def test_pinned_pdfs_never_count_or_go():
    """However many pinned PDFs, and however old, they are kept, and don't
    push auto-cached ones out."""
    r = _lru(_fill(MAX, list(range(1, 51))) + """
await L.evictIfNeeded();
console.log(JSON.stringify(Object.keys(__state().lru).length));
""")
    assert r == MAX + 50


def test_held_pdfs_never_count_or_go():
    """A PDF a setlist holds is kept like a pinned one."""
    r = _lru(_fill(MAX + 1, [], held=[1, 2]) + """
await L.evictIfNeeded();
console.log(JSON.stringify(Object.keys(__state().lru)));
""")
    assert "/h1.pdf" in r and "/h2.pdf" in r
    assert "/u0.pdf" not in r and len(r) == MAX + 2


def test_a_touch_keeps_held():
    """The service worker touches a PDF on every view and revalidation;
    that must not release a setlist's hold."""
    r = _lru("""
await L.touchLruEntry("/a.pdf", 5, false);
await L.setHeld(new Set(["/a.pdf"]));
__setNow(2000); await L.touchLruEntry("/a.pdf", 7, false);
console.log(JSON.stringify(__state().lru));
""")
    assert r == {"/a.pdf": {"size": 7, "pinned": False, "lastUsed": 2000, "held": True}}


def test_set_held_marks_and_releases_without_touching_the_rest():
    r = _lru("""
await L.touchLruEntry("/a.pdf", 1, true);
__setNow(2000); await L.touchLruEntry("/b.pdf", 2, false);
__setNow(3000); await L.touchLruEntry("/c.pdf", 3, false);
await L.setHeld(new Set(["/a.pdf", "/b.pdf", "/not-cached.pdf"]));
__setNow(9000);
await L.setHeld(new Set(["/a.pdf", "/c.pdf"]));    // /b released, /c held
console.log(JSON.stringify(__state().lru));
""")
    assert r == {
        "/a.pdf": {"size": 1, "pinned": True, "lastUsed": 1000, "held": True},
        "/b.pdf": {"size": 2, "pinned": False, "lastUsed": 2000},
        "/c.pdf": {"size": 3, "pinned": False, "lastUsed": 3000, "held": True},
    }


def test_unpin_manual_keeps_the_copy_and_the_hold():
    r = _lru("""
await L.touchLruEntry("/a.pdf", 1, true);
await L.touchLruEntry("/b.pdf", 1, true);
await L.setHeld(new Set(["/b.pdf"]));
await L.unpinManual("/a.pdf");
await L.unpinManual("/b.pdf");
await L.unpinManual("/none.pdf");
console.log(JSON.stringify(__state().lru));
""")
    assert r == {
        "/a.pdf": {"size": 1, "pinned": False, "lastUsed": 1000},
        "/b.pdf": {"size": 1, "pinned": False, "lastUsed": 1000, "held": True},
    }


@pytest.mark.parametrize("path", ["/m/Bach - Suite.pdf", "/m/Dvořák & Sons #1?.pdf", "/m/100% ok+.pdf"])
def test_cache_key(path):
    assert _lru(f"console.log(JSON.stringify(L.pdfCacheKey({json.dumps(path)})));") == _key(path)


# ---------------------------------------------------------------------------
# The real service worker
# ---------------------------------------------------------------------------


def _sw(body: str):
    """Evaluate the real sw.js as a classic worker (importScripts reads
    files from web/static), then run *body*."""
    return run_deno(FAKES + f"""
const STATIC = {json.dumps(str(STATIC))};
const listeners = {{}};
globalThis.self = globalThis;
self.addEventListener = (ev, fn) => {{ listeners[ev] = fn; }};
self.skipWaiting = () => {{}};
self.clients = {{ claim: async () => {{}}, matchAll: async () => [] }};
globalThis.__imported = [];
self.importScripts = (...urls) => {{
  for (const url of urls) {{
    __imported.push(url);
    (0, eval)(Deno.readTextFileSync(STATIC + new URL(url, location.href).pathname));
  }}
}};
(0, eval)(Deno.readTextFileSync(STATIC + "/sw.js"));
const install = async () => {{ let w; listeners.install({{ waitUntil: (p) => {{ w = p; }} }}); await w; }};
const fetchPdf = async (path) => {{
  let resp;
  listeners.fetch({{
    request: new Request("/api/pdf?path=" + encodeURIComponent(path)),
    respondWith: (p) => {{ resp = p; }}, waitUntil: () => {{}},
  }});
  const r = await resp;
  await __tick(60);
  return r.status;
}};
{body}
""", location="https://folio.test")


def test_service_worker_loads_the_shared_script_and_installs():
    r = _sw("""
await install();
console.log(JSON.stringify({ imported: __imported, events: Object.keys(listeners).sort(),
  shell: __state().stores }));
""")
    assert len(r["imported"]) == 1 and r["imported"][0].startswith("/modules/offline-lru.js?v=")
    assert r["events"] == ["activate", "fetch", "install", "message"]
    [shell] = [v for k, v in r["shell"].items() if k.startswith("folio-v")]
    assert "/modules/offline-lru.js" in shell


def test_service_worker_caches_a_viewed_pdf_as_auto_cached():
    path = "/m/Dvořák & Sons.pdf"
    r = _sw(f"""
const status = await fetchPdf({json.dumps(path)});
console.log(JSON.stringify({{ status, ...__state() }}));
""")
    assert r["status"] == 200
    assert r["stores"]["folio-pdfs-v2"] == [_key(path)]
    assert r["lru"] == {path: {"size": len("%PDF-1.4 https://folio.test" + _key(path)), "pinned": False,
                               "lastUsed": 1000}}


def test_service_worker_serves_a_cached_pdf_and_marks_it_used():
    path = "/m/Bach - Suite.pdf"
    r = _sw(f"""
await fetchPdf({json.dumps(path)});
__setNow(5000);
const status = await fetchPdf({json.dumps(path)});
const e = __state().lru[{json.dumps(path)}];
console.log(JSON.stringify({{ status, lastUsed: e.lastUsed, pinned: e.pinned,
  cached: __state().stores["folio-pdfs-v2"] }}));
""")
    assert r["status"] == 200
    assert r["lastUsed"] == 5000
    assert r["pinned"] is False                 # viewing never pins
    assert r["cached"] == [_key(path)]


def test_viewing_a_cached_pdf_offline_still_counts_as_a_use():
    """Offline the background refresh fails, so the cache hit itself must
    record the use, or eviction would treat PDFs read offline as unused."""
    path = "/m/Bach - Suite.pdf"
    r = _sw(f"""
await fetchPdf({json.dumps(path)});
globalThis.fetch = async () => {{ throw new TypeError("offline"); }};
__setNow(7000);
const status = await fetchPdf({json.dumps(path)});
console.log(JSON.stringify({{ status, lastUsed: __state().lru[{json.dumps(path)}].lastUsed }}));
""")
    assert r == {"status": 200, "lastUsed": 7000}


def test_service_worker_keeps_a_pinned_pdf_pinned_when_viewed():
    path = "/m/Bach - Suite.pdf"
    r = _sw(f"""
await self.FolioLru.touchLruEntry({json.dumps(path)}, 9, true);
await fetchPdf({json.dumps(path)});
console.log(JSON.stringify(__state().lru[{json.dumps(path)}].pinned));
""")
    assert r is True


def test_service_worker_reports_the_lru_schema_the_page_syncs_under():
    """cache.js syncs only when the worker's reply carries the page's
    LRU_SCHEMA: a reply without it would stop every device syncing."""
    r = _sw("""
let reply = null;
listeners.message({ data: { type: "GET_VERSION" }, ports: [{ postMessage: (m) => { reply = m; } }] });
console.log(JSON.stringify({ reply, schema: self.FolioLru.LRU_SCHEMA }))
""")
    assert r["reply"]["lruSchema"] == r["schema"] == 2
    assert r["reply"]["version"]


def test_service_worker_keeps_a_held_pdf_held_when_viewed_at_the_limit():
    """Viewing a setlist PDF touches its entry (and refreshes it in the
    background); the touch must keep `held`, or the next store evicts it.
    The clock is set so the touched entry is still the oldest."""
    r = _sw("""
const L = self.FolioLru;
await __seedCached("/held.pdf", 1, { held: true });
for (let i = 0; i < L.MAX_AUTO_CACHED; i++) await __seedCached(`/u${i}.pdf`, 10 + i);
__setNow(5);
await fetchPdf("/held.pdf");
await fetchPdf("/new.pdf");
await __tick(100);
console.log(JSON.stringify(__state().lru["/held.pdf"] || null));
""")
    assert r is not None and r.get("held") is True


# ---------------------------------------------------------------------------
# The page (cache.js)
# ---------------------------------------------------------------------------


def _page(body: str):
    return run_module(MODULES / "cache.js", ["dom"], body + "\nawait __tick();",
                      setup="globalThis.window = globalThis; globalThis.isSecureContext = true;\n" + FAKES)


def test_page_downloads_and_pins_a_pdf():
    path = "/m/Bach - Suite.pdf"
    r = _page(f"""
await M.cachePdf({json.dumps(path)});
console.log(JSON.stringify({{ ...__state(), fetched: __fetched, button: M.cacheButtonHtml({json.dumps(path)}) }}));
""")
    assert r["stores"][r"folio-pdfs-v2"] == [_key(path)]
    assert r["lru"][path]["pinned"] is True
    assert r["fetched"][0].startswith(_key(path) + "&_t=")
    assert "Pinned for offline use" in r["button"]


def test_page_pins_an_auto_cached_pdf_without_downloading_it_again():
    path = "/m/Bach - Suite.pdf"
    r = _page(f"""
await self.FolioLru.touchLruEntry({json.dumps(path)}, 42, false);
(await caches.open(M.PDF_CACHE)).put(M.pdfCacheKey({json.dumps(path)}), new Response("x"));
await M.pinPdf({json.dumps(path)});
console.log(JSON.stringify({{ ...__state(), fetched: __fetched }}));
""")
    assert r["fetched"] == []
    assert r["lru"][path] == {"size": 42, "pinned": True, "lastUsed": 1000}


def test_page_unpin_keeps_the_pdf_as_auto_cached():
    """Unpinning no longer deletes: a Recent PDF would only be downloaded
    again by the next sync."""
    path = "/m/Bach - Suite.pdf"
    r = _page(f"""
await M.cachePdf({json.dumps(path)});
await M.unpinPdf({json.dumps(path)});
console.log(JSON.stringify({{ ...__state(), button: M.cacheButtonHtml({json.dumps(path)}) }}));
""")
    assert r["lru"][path]["pinned"] is False
    assert r["stores"]["folio-pdfs-v2"] == [_key(path)]
    assert "Auto-cached" in r["button"]


def test_page_uses_the_shared_cache_name_and_keys():
    """cache.js re-exports the shared ones rather than keeping its own, so
    the page finds and removes what the service worker cached."""
    r = _page("""
console.log(JSON.stringify({ cache: M.PDF_CACHE, same: self.FolioLru.PDF_CACHE === M.PDF_CACHE,
  key: M.pdfCacheKey === self.FolioLru.pdfCacheKey }));
""")
    assert r == {"cache": "folio-pdfs-v2", "same": True, "key": True}
