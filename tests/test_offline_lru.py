"""Offline PDF cache bookkeeping, shared by the service worker and the page.

web/static/modules/offline-lru.js (Step 10a) is the one copy of the cache
name, the PDF cache keys and the "folio-lru-v2" IndexedDB store: when each
cached PDF was last used, its size, and whether the user pinned it. Only
unpinned (auto-cached) PDFs are evicted, oldest first, beyond
MAX_AUTO_CACHED. sw.js loads it with importScripts(); cache.js imports it.

These run it the three ways it is used, under Deno with in-memory
IndexedDB and Cache Storage: its functions directly; the REAL sw.js,
installing and serving PDFs; and the page's cachePdf/pinPdf/evictPdf.
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
globalThis.__state = () => ({
  lru: Object.fromEntries([...__lru].map(([p, e]) => [p, { size: e.size, pinned: e.pinned, lastUsed: e.lastUsed }])),
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


def _fill(unpinned: int, pinned: list[int]) -> str:
    """Cache `unpinned` auto-cached PDFs /u<i>.pdf used at time i, plus pinned
    PDFs /p<i>.pdf used at the given (old) times."""
    return f"""
const cache = await caches.open(L.PDF_CACHE);
for (let i = 0; i < {unpinned}; i++) {{
  __setNow(10 + i); await L.touchLruEntry(`/u${{i}}.pdf`, 1, false);
  await cache.put(L.pdfCacheKey(`/u${{i}}.pdf`), new Response("x"));
}}
for (const t of {json.dumps(pinned)}) {{
  __setNow(t); await L.touchLruEntry(`/p${{t}}.pdf`, 1, true);
  await cache.put(L.pdfCacheKey(`/p${{t}}.pdf`), new Response("x"));
}}
"""


def test_eviction_removes_the_oldest_unpinned_beyond_the_limit():
    r = _lru(_fill(103, [1, 2]) + """
await L.evictIfNeeded();
const s = __state();
console.log(JSON.stringify({ max: L.MAX_AUTO_CACHED, lru: Object.keys(s.lru), cached: s.stores[L.PDF_CACHE] }));
""")
    assert r["max"] == 100
    kept = [f"/u{i}.pdf" for i in range(3, 103)] + ["/p1.pdf", "/p2.pdf"]
    assert sorted(r["lru"]) == sorted(kept)
    assert r["cached"] == sorted(_key(p) for p in kept)     # evicted PDFs leave the cache too


def test_at_the_limit_nothing_is_evicted():
    r = _lru(_fill(100, []) + """
await L.evictIfNeeded();
console.log(JSON.stringify(Object.keys(__state().lru).length));
""")
    assert r == 100


def test_pinned_pdfs_never_count_or_go():
    """However many pinned PDFs, and however old, they are kept, and don't
    push auto-cached ones out."""
    r = _lru(_fill(100, list(range(1, 51))) + """
await L.evictIfNeeded();
console.log(JSON.stringify(Object.keys(__state().lru).length));
""")
    assert r == 150


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


def test_page_eviction_removes_the_pdf_and_its_entry():
    path = "/m/Bach - Suite.pdf"
    r = _page(f"""
await M.cachePdf({json.dumps(path)});
await M.evictPdf({json.dumps(path)});
console.log(JSON.stringify({{ ...__state(), button: M.cacheButtonHtml({json.dumps(path)}) }}));
""")
    assert r["lru"] == {} and r["stores"]["folio-pdfs-v2"] == []
    assert "Download for offline use" in r["button"]


def test_page_uses_the_shared_cache_name_and_keys():
    """cache.js re-exports the shared ones rather than keeping its own, so
    the page finds and removes what the service worker cached."""
    r = _page("""
console.log(JSON.stringify({ cache: M.PDF_CACHE, same: self.FolioLru.PDF_CACHE === M.PDF_CACHE,
  key: M.pdfCacheKey === self.FolioLru.pdfCacheKey }));
""")
    assert r == {"cache": "folio-pdfs-v2", "same": True, "key": True}
